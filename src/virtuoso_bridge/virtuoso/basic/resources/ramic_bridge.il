;RB = RAMIC Bridge
;RBD = RB Daemon
;RBM = RB Monitor

; Configuration variables for the RAMIC Bridge
; Load RBDPath from environment variable RB_DAEMON_PATH, fallback to default if not set
RBDPath = if(getShellEnvVar("RB_DAEMON_PATH") then
                getShellEnvVar("RB_DAEMON_PATH")
            else
                "./ramic_bridge_daemon.py"
            )
; Load RBPython from environment variable RB_PYTHON_PATH, fallback to "python" if not set
RBPython = if(getShellEnvVar("RB_PYTHON_PATH") then
                getShellEnvVar("RB_PYTHON_PATH")
            else
                "python"
            )
RBPort = if(getShellEnvVar("RB_PORT") then
                atoi(getShellEnvVar("RB_PORT"))
            else
                65432
            )                                                                      ; TCP port for client connections
RBLocal = nil                                                                      ; Local-only connections (127.0.0.1 vs 0.0.0.0)
RBEcho = nil                                                                     ; Echo bridging messages for debugging
RBDLog = nil                                                                     ; Enable daemon logging to /tmp/RB.log

; IPC process handle. Do not reset on reload: a live daemon keeps the port;
; clearing RBIpc would make RBStart spawn a second daemon -> bind failure (exit 1).
unless(boundp('RBIpc) RBIpc = 'unbound)

; Ring buffer for daemon stderr; flushed to CIW only on non-zero exit.
unless(boundp('RBStderrBuf) RBStderrBuf = "")

procedure(RBIpcDataHandler(ipcId data)
  ; Handler for successful data received from the Python daemon.
  ; Code is executed via evalstring(), which bypasses CIW's interactive loop.
  ; As a result, CIW output (printf) is line-buffered: only text ending with
  ; "\n" is flushed immediately.  Without "\n", output stays in the buffer
  ; until the next newline or explicit flush.  This differs from typing
  ; directly in CIW, where the interactive loop flushes after every command.
  let((result)
    when(RBEcho printf("[RAMIC Bridge (%L)] receive:%L\n" ipcId data))
    if(errset(result=evalstring(data)) then
	    ; Success: Send response with STX (0x02) start marker and RS (0x1E) end marker
	    ipcWriteProcess(ipcId sprintf(nil "%c%L%c" intToChar(2) result intToChar(30)))
		when(RBEcho printf("[RAMIC Bridge (%L)] return:%L\n" ipcId result))
	else
        ; Error: NAK + payload to Python only (no CIW printf; client parses errors)
        ipcWriteProcess(ipcId sprintf(nil "%c%L%c" intToChar(21) errset.errset intToChar(30)))
    )
  )
)

procedure(RBIpcErrHandler(ipcId data)
    ; Buffer daemon stderr; emitted to CIW only on non-zero exit
    ; (RBIpcFinishHandler). Avoids flooding CIW with routine startup
    ; chatter / Python warnings while preserving fatal-error context.
    RBStderrBuf = strcat(RBStderrBuf data)
    when(strlen(RBStderrBuf) > 8192
        RBStderrBuf = substring(RBStderrBuf strlen(RBStderrBuf)-8191 8192))
)

procedure(RBIpcFinishHandler(ipcId data)
  ; Handler called when the Python daemon process exits
  let((exitStatus)
	exitStatus = ipcGetExitStatus(ipcId)
    printf("[RAMIC Bridge (%L)] exit at (%s) with state = %L\n" ipcId getCurrentTime() exitStatus)
    when(exitStatus != 0 && strlen(RBStderrBuf) > 0
        printf("[RAMIC Bridge stderr]\n%s\n" RBStderrBuf))
    RBStderrBuf = ""
  )
)

procedure(RBStart()
    ; Start the RAMIC Bridge daemon process
    ; This launches the Python daemon as a child process of Virtuoso
	if(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) then
		printf("[RAMIC Bridge (%L)] is already running\n", RBIpc)
	else
		prog((host logpath)
			; Determine host binding based on RBLocal setting
			if(RBLocal then
				host = "127.0.0.1"  ; Local-only connections
			else
				host = "0.0.0.0"   ; Accept connections from any IP
			)
			; Set log path if logging is enabled
			if(RBDLog then
				logpath = "/tmp/RB.log"
			else
				logpath = ""
			)
			; Start the Python daemon process with IPC handlers.
			; Use "env -u" to strip Virtuoso's LD_LIBRARY_PATH / LD_PRELOAD
			; so the system Python links against its own libs, not Virtuoso's
			; bundled (and often incompatible) copies.
			RBIpc = ipcBeginProcess(sprintf(nil "/usr/bin/env -u LD_LIBRARY_PATH -u LD_PRELOAD stdbuf -oL %s -u %L %L %L" RBPython RBDPath host RBPort) "" 'RBIpcDataHandler 'RBIpcErrHandler 'RBIpcFinishHandler logpath)
		)
		printf("[RAMIC Bridge (%L)] start at (%s)\n" RBIpc getCurrentTime())
	)
)

procedure(RBStop()
    ; Stop the RAMIC Bridge daemon process
	if(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) then
		ipcKillProcess(RBIpc)
	else
		printf("[RAMIC Bridge] is already down\n" )
	)
)

procedure(RBStopAll()
    ; Emergency function to kill all RAMIC Bridge daemon processes
    ; 1. Release Virtuoso's IPC bookkeeping for the live handle (if any)
    ; 2. Match daemon by script filename (ramic_bridge_daemon_[23]),
    ;    not by interpreter name -- works with pypy, conda pythons, etc.
    ; 3. SIGTERM, wait 2s, then SIGKILL holdouts
    ; 4. Free the port: fuser if available, otherwise surface the holder
    ;    via ss/lsof so the user can see what to kill
    ; 5. Reset Skill-side IPC state and stderr buffer
    if(boundp('RBIpc) && RBIpc != 'unbound then
        errset(ipcKillProcess(RBIpc)))
    sh(sprintf(nil
        "pgrep -f 'ramic_bridge_daemon_[23]' | xargs -r kill; sleep 2; pgrep -f 'ramic_bridge_daemon_[23]' | xargs -r kill -9; if command -v fuser >/dev/null 2>&1; then fuser -k %d/tcp 2>/dev/null; elif command -v ss >/dev/null 2>&1; then ss -tlnp 2>/dev/null | awk -v p=%d '$4 ~ \":\"p\"$\"' >&2; elif command -v lsof >/dev/null 2>&1; then lsof -iTCP:%d -sTCP:LISTEN >&2; fi"
        RBPort RBPort RBPort))
    RBIpc = 'unbound
    RBStderrBuf = ""
    printf("[RAMIC Bridge] RBStopAll: kill issued; verify with: ss -tlnp | grep %d (or lsof -iTCP:%d)\n" RBPort RBPort)
)

; ============================================================================
; GUI Components for the RAMIC Bridge Monitor (once per session)
; ============================================================================
; Re-running hiCreateAppForm / hiInsertBannerMenu on reload causes form/menu
; conflicts; build GUI and banner menu only the first time.
unless(boundp('RBMonInstalled)
progn(
; Status label showing current bridge state
RBMState = hiCreateLabel(
	?name 		'RBMState
	?labelText 	"RAMIC Bridge State: ?"
	?justification CDS_JUSTIFY_LEFT
)

; Refresh button to update status display
RBMBtnRefresh = hiCreateButton(
	?name		'RBMBtnRefresh
	?buttonText	"Refresh"
	?callback	"RBMRefresh()"
)

; Start bridge button
RBMBtnStart = hiCreateButton(
	?name		'RBMBtnStart
	?buttonText	"Start"
	?callback	"RBStart() RBMRefresh()"
)

; Stop bridge button
RBMBtnStop = hiCreateButton(
	?name		'RBMBtnStop
	?buttonText	"Stop"
	?callback	"RBStop() RBMRefresh()"
)

; Port number input field
RBMIntPort = hiCreateIntField(
	?name		'RBMIntPort
	?prompt		"Port"
	?value		RBPort
	?defValue	RBPort
	?callback	nil
)

; Local-only connection checkbox
RBMBolLocal = hiCreateBooleanButton(
	?name		'RBMBolLocal
	?buttonText	"Local connect only"
	?value		RBLocal
	?defValue	RBLocal
	?callback	nil
)

; Echo bridging messages checkbox
RBMBolEcho = hiCreateBooleanButton(
	?name		'RBMBolEcho
	?buttonText	"Echo bridging string"
	?value		RBEcho
	?defValue	RBEcho
	?callback	nil
)

; Enable daemon logging checkbox
RBMBolLog = hiCreateBooleanButton(
	?name		'RBMBolLog
	?buttonText	"Daemon log (/tmp/RB.log)"
	?value		RBDLog
	?defValue	RBDLog
	?callback	nil
)

; Emergency kill all daemons button
RBMBtnStopAll = hiCreateButton(
	?name		'RBMBtnStopAll
	?buttonText	"KILL ALL DAEMON (USE AS BACKUP ONLY)"
	?callback	"RBStopAll() RBMRefresh()"
)

; Create the main monitor form
hiCreateAppForm(
	?name 'RBMonitor
	?formTitle "RAMIC Bridge Monitor"
	?fields list(
		list(RBMState		20:10	300:30)    ; Status label
		list(RBMBtnRefresh 	20:50 	100:30)    ; Refresh button
		list(RBMBtnStart  	150:50 	100:30)    ; Start button
		list(RBMBtnStop  	280:50 	100:30)    ; Stop button
		list(RBMIntPort		20:90	170:30 	30)  ; Port input
		list(RBMBolLocal	20:130	170:30)     ; Local-only checkbox
		list(RBMBolEcho		20:170	170:30)     ; Echo checkbox
		list(RBMBolLog		20:210	170:30)     ; Log checkbox
		list(RBMBtnStopAll 	20:250 	360:30)     ; Emergency stop button
	)
	?buttonLayout	'OKCancelApply
	?help			""
	?initialSize		400:320
	?minSize			400:320
	?maxSize			400:320
	?mapCB			"RBMRefresh()"
	?callback 		"RBMApply()"
)

; ============================================================================
; Menu Integration
; ============================================================================

; Create RAMIC menu in Virtuoso's banner
ramicMenu = hiCreatePulldownMenu(
	'ramicMenu
	"RAMIC"
	list(
		hiCreateMenuItem(
			?name 'RAMIC_Bridge
			?itemText "RAMIC Bridge..."
			?callback "hiDisplayForm(RBMonitor)"
		)
		hiCreateMenuItem(
			?name 'RAMIC_PrintLines
			?itemText "Print Empty Lines"
			?callback "RBPrintEmptyLines()"
		)
	)
)

; Insert the RAMIC menu into Virtuoso's main window
hiInsertBannerMenu(window(1) ramicMenu 3)
RBMonInstalled = t
)
) ; end unless(boundp 'RBMonInstalled) + progn

procedure(RBMRefresh()
    ; Refresh the monitor display with current status and settings
	when(boundp('RBMonInstalled)
	if(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) then
		RBMonitor->RBMState->value = sprintf(nil "RAMIC Bridge State: Running (%L)" RBIpc)
	else
		RBMonitor->RBMState->value = "RAMIC Bridge State: Down"
	)
	RBMonitor->RBMIntPort->value = RBPort
	RBMonitor->RBMBolLocal->value = RBLocal
	RBMonitor->RBMBolEcho->value = RBEcho
	RBMonitor->RBMBolLog->value = RBDLog
	)
)

procedure(RBMApply()
    ; Apply changes from the monitor form to the configuration
	when(boundp('RBMonInstalled)
	RBEcho = RBMonitor->RBMBolEcho->value
	prog((refresh)
	unless(RBMonitor->RBMBolLocal->value == RBLocal
		RBLocal = RBMonitor->RBMBolLocal->value
		refresh = t
	)
	unless(RBMonitor->RBMIntPort->value == RBPort
		RBPort = RBMonitor->RBMIntPort->value
		refresh = t
	)
	unless(RBMonitor->RBMBolLog->value == RBDLog
		RBDLog = RBMonitor->RBMBolLog->value
		refresh = t
	)
	when(refresh
		RBStop()
		RBStart()
	)
	)
	)
)

procedure(RBPrintEmptyLines()
	printf("\n\n\n\n\n\n\n\n\n\n\n\n")
)

; Auto-start the bridge when this file is loaded (no second daemon if already up)
RBStart()
