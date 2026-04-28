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

; Last known daemon OS pid + bind address + remote hostname.  Populated
; by RBIpcErrHandler when it parses the daemon's startup banner; reused
; by Refresh / Finish / Stop so post-mortem messages keep showing
; pid/host/port even after the IPC handle has been freed.
unless(boundp('RBLastPid) RBLastPid = nil)
unless(boundp('RBLastBind) RBLastBind = "")
unless(boundp('RBLastHost) RBLastHost = "")
unless(boundp('RBLastIP) RBLastIP = "")

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
    ; Parse the daemon's startup banner ("[RB-banner] pid=N bind=H:P")
    ; from stderr.  Plain SKILL string ops only -- pcreSubstring is
    ; not available on every IC6.1.x install, and SKILL-level ``index``
    ; returns the matched suffix (not a position).  We tokenise on
    ; whitespace and pluck the pid=/bind= tokens.
    ;
    ; Note: SKILL string-literal escapes (\t, \n) are not honoured
    ; consistently across IC builds when used inside parseString's
    ; separator argument -- on this IC6.1.8 install ``" \t\n"`` does
    ; NOT split on newline.  We build the separator with explicit
    ; char codes (TAB=9, LF=10) so behaviour is identical everywhere.
    let((sep saw_banner prevPid)
        sep = strcat(" "
                     buildString(list(intToChar(9)))
                     buildString(list(intToChar(10))))
        prevPid = RBLastPid
        saw_banner = member("[RB-banner]" parseString(data sep))
        when(saw_banner
            foreach(tok parseString(data sep)
                when(strlen(tok) > 4 && equal(substring(tok 1 4) "pid=")
                    RBLastPid = atoi(substring(tok 5)))
                when(strlen(tok) > 5 && equal(substring(tok 1 5) "bind=")
                    RBLastBind = substring(tok 6))
                when(strlen(tok) > 5 && equal(substring(tok 1 5) "host=")
                    RBLastHost = substring(tok 6))
                when(strlen(tok) > 3 && equal(substring(tok 1 3) "ip=")
                    RBLastIP = substring(tok 4)))
            ; First banner of this daemon's lifetime --> announce
            ; "ready" with the populated identity.  RBStart printed
            ; only "launching" earlier; this is the authoritative
            ; startup ack and avoids the pid=nil race window.
            when(RBLastPid && !prevPid
                printf("[RAMIC Bridge ipc=%L %s pid=%L] ready: bind=%s log=%s\n"
                    ipcId
                    sprintf(nil "%s (%s)" RBLastIP RBLastHost)
                    RBLastPid RBLastBind
                    if(RBDLog "/tmp/RB.log" "off")))
            ; Banner is the canonical "daemon just identified itself"
            ; event -- repaint the monitor so users don't have to hit
            ; Refresh after a fresh start.
            when(boundp('RBMonInstalled) errset(RBMRefresh())))
    )
)

procedure(RBIpcFinishHandler(ipcId data)
  ; Handler called when the Python daemon process exits
  let((exitStatus)
	exitStatus = ipcGetExitStatus(ipcId)
    printf("[RAMIC Bridge ipc=%L pid=%L port=%d] exit at (%s) state=%L\n"
        ipcId RBLastPid RBPort getCurrentTime() exitStatus)
    when(exitStatus != 0 && strlen(RBStderrBuf) > 0
        printf("[RAMIC Bridge stderr]\n%s\n" RBStderrBuf))
    RBStderrBuf = ""
    ; Daemon is gone -- repaint the monitor so it shows Down without
    ; the user having to click Refresh.
    when(boundp('RBMonInstalled) errset(RBMRefresh()))
  )
)

procedure(RBStart()
    ; Start the RAMIC Bridge daemon process.  Launches the Python daemon
    ; as a child process of Virtuoso; the actual "ready" ack is printed
    ; by RBIpcErrHandler when it sees the daemon's [RB-banner] line on
    ; stderr (which is also where RBLastPid / RBLastBind get populated).
    ; Doing it this way avoids the pid=nil race -- the startup printf
    ; would otherwise run before the banner has been read off stderr.

    ; Zombie scrub: if SKILL thinks the IPC handle is alive but no
    ; banner has ever been parsed for it (RBLastPid is nil), the daemon
    ; almost certainly died without releasing the IPC bookkeeping.
    ; A real, healthy daemon populates RBLastPid via stderr banner
    ; within ~hundreds of ms of ipcBeginProcess returning.  Reset SKILL
    ; state so we don't loop forever printing "already running" against
    ; a dead daemon.
    when(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) && !RBLastPid
        printf("[RAMIC Bridge] zombie detected (ipc %L alive, no banner); resetting\n" RBIpc)
        errset(ipcKillProcess(RBIpc))
        RBIpc = 'unbound
        RBLastBind = ""
        RBLastHost = "")

    if(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) then
        printf("[RAMIC Bridge ipc=%L pid=%L port=%d] already running\n"
            RBIpc RBLastPid RBPort)
    else
        ; Clear leftover banner state so the new daemon's banner is
        ; recognised as the first-of-its-lifetime by RBIpcErrHandler.
        RBLastPid = nil
        RBLastBind = ""
        RBLastHost = ""
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
        printf("[RAMIC Bridge ipc=%L] launching daemon (bind=%s:%d) at (%s)\n"
            RBIpc
            if(RBLocal "127.0.0.1" "0.0.0.0")
            RBPort
            getCurrentTime())
    )
)

procedure(RBStop()
    ; Stop the RAMIC Bridge daemon process
	if(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) then
		printf("[RAMIC Bridge ipc=%L pid=%L port=%d] stopping\n"
			RBIpc RBLastPid RBPort)
		ipcKillProcess(RBIpc)
	else
		printf("[RAMIC Bridge] already down\n")
	)
)

procedure(RBStopAll()
    ; Emergency function to kill **the current user's** RAMIC Bridge
    ; daemon processes only.  Earlier versions used ``pgrep -f`` without
    ; ``-u``, which scanned every UID on the host and tried to kill
    ; other users' daemons -- yielding "permission denied" noise and
    ; (worst case) terminating an unrelated user's bridge if both ran
    ; under the same account.
    ;
    ; 1. Release Virtuoso's IPC bookkeeping for the live handle (if any)
    ; 2. Match daemon processes belonging to ``$USER`` only, by script
    ;    filename (ramic_bridge_daemon_[23]).
    ; 3. SIGTERM, wait 2s, then SIGKILL holdouts.
    ; 4. Free the port: fuser only kills sockets the caller owns; ss/lsof
    ;    fallbacks just *show* the holder.
    ; 5. Reset Skill-side IPC state and stderr buffer.
    if(boundp('RBIpc) && RBIpc != 'unbound then
        errset(ipcKillProcess(RBIpc)))
    sh(sprintf(nil
        "echo \"[RAMIC StopAll] user=$USER scope=own-uid port=%d\" >&2; pids=$(pgrep -u \"$USER\" -f 'ramic_bridge_daemon_[23]'); if [ -n \"$pids\" ]; then echo \"[RAMIC StopAll] killing pids: $pids\" >&2; echo \"$pids\" | xargs -r kill; sleep 2; pids2=$(pgrep -u \"$USER\" -f 'ramic_bridge_daemon_[23]'); [ -n \"$pids2\" ] && echo \"$pids2\" | xargs -r kill -9; else echo \"[RAMIC StopAll] no own daemon processes\" >&2; fi; if command -v fuser >/dev/null 2>&1; then fuser -k %d/tcp 2>/dev/null; elif command -v ss >/dev/null 2>&1; then ss -tlnp 2>/dev/null | awk -v p=%d '$4 ~ \":\"p\"$\"' >&2; elif command -v lsof >/dev/null 2>&1; then lsof -iTCP:%d -sTCP:LISTEN >&2; fi"
        RBPort RBPort RBPort RBPort))
    RBIpc = 'unbound
    RBLastPid = nil
    RBLastHost = ""
    RBStderrBuf = ""
    printf("[RAMIC Bridge] RBStopAll: killed own daemons (UID-scoped, port=%d); verify: ss -tlnp | grep %d\n"
        RBPort RBPort)
)

; ============================================================================
; GUI Components for the RAMIC Bridge Monitor (once per session)
; ============================================================================
; Re-running hiCreateAppForm / hiInsertBannerMenu on reload causes form/menu
; conflicts; build GUI and banner menu only the first time.
unless(boundp('RBMonInstalled)
progn(
; Status label showing current bridge state.  RBMRefresh() rewrites this
; with live ipc / pid / bind info so the GUI tells you what's actually
; serving — instead of just "Running" with no context.
RBMState = hiCreateLabel(
	?name 		'RBMState
	?labelText 	"RAMIC Bridge: (press Refresh status)"
	?justification CDS_JUSTIFY_LEFT
)

; Refresh button — only re-reads the SKILL state (RBIpc / RBPort /
; RBLocal / RBEcho / RBDLog / pid) and repaints this form.  It does not
; restart the daemon and does not re-read /tmp socket state.
RBMBtnRefresh = hiCreateButton(
	?name		'RBMBtnRefresh
	?buttonText	"Refresh status"
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

; Emergency kill button — UID-scoped (only kills this user's daemons).
; Earlier "KILL ALL" was misleading: it tried to kill every user's
; daemon on the host and got "permission denied" on others.
RBMBtnStopAll = hiCreateButton(
	?name		'RBMBtnStopAll
	?buttonText	"Kill MY daemons (force, UID-scoped)"
	?callback	"RBStopAll() RBMRefresh()"
)

; Create the main monitor form.  Note: form geometry is fixed at the
; first ``hiCreateAppForm`` call in this Virtuoso session — re-loading
; ramic_bridge.il only redefines the procedures (RBMRefresh / RBStart /
; RBStopAll), it does NOT resize the existing form.  To pick up new
; geometry, restart Virtuoso (or unbind RBMonInstalled and reload).
hiCreateAppForm(
	?name 'RBMonitor
	?formTitle "RAMIC Bridge Monitor"
	?fields list(
		list(RBMState		20:10	600:30)     ; Status label (wide for ipc/pid/bind line)
		list(RBMBtnRefresh 	20:50 	130:30)     ; Refresh button
		list(RBMBtnStart  	170:50 	100:30)     ; Start button
		list(RBMBtnStop  	290:50 	100:30)     ; Stop button
		list(RBMIntPort		20:90	170:30 	30) ; Port input
		list(RBMBolLocal	20:130	250:30)     ; Local-only checkbox
		list(RBMBolEcho		20:170	250:30)     ; Echo checkbox
		list(RBMBolLog		20:210	250:30)     ; Log checkbox
		list(RBMBtnStopAll 	20:250 	370:30)     ; Emergency stop button
	)
	?buttonLayout	'OKCancelApply
	?help			""
	?initialSize		640:320
	?minSize			640:320
	?maxSize			640:320
	?mapCB			"RBMRefresh()"
	?callback 		"RBMApply()"
)

; ============================================================================
; Menu Integration
; ============================================================================

; Create RAMIC menu in Virtuoso's banner.  Note: menu and form are
; built only once per session (gated by RBMonInstalled); reloading
; ramic_bridge.il updates the procedure bodies but cannot add new
; menu entries without restarting Virtuoso.  In the meantime the
; functions are still callable from the CIW directly.
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
			?name 'RAMIC_PrintLines5
			?itemText "Print 5 Empty Lines"
			?callback "RBPrintEmptyLines(5)"
		)
		hiCreateMenuItem(
			?name 'RAMIC_PrintLines10
			?itemText "Print 10 Empty Lines"
			?callback "RBPrintEmptyLines(10)"
		)
	)
)

; Insert the RAMIC menu into Virtuoso's main window
hiInsertBannerMenu(window(1) ramicMenu 3)
RBMonInstalled = t
)
) ; end unless(boundp 'RBMonInstalled) + progn

procedure(RBMRefresh()
    ; Re-read SKILL state and repaint the monitor form.
    ; - State label gets ipc handle + os pid + bind:port so debugging
    ;   doesn't require a separate `ss -tlnp` round-trip.
    ; - Other widgets just mirror the current SKILL globals.
	when(boundp('RBMonInstalled)
	if(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) then
		prog((bind hostStr)
			; Re-derive bind from current settings (in case RBLocal toggled
			; without a daemon restart).  RBLastBind is the bind captured
			; at start; prefer it because it reflects what the daemon
			; *actually* listens on.
			bind = if(RBLastBind && strlen(RBLastBind) > 0
				RBLastBind
				sprintf(nil "%s:%d"
					if(RBLocal "127.0.0.1" "0.0.0.0")
					RBPort))
			; Identity column: prefer "IP (hostname)"; fall back to
			; whatever we have if the banner was older / partial.
			hostStr = cond(
				((strlen(RBLastIP) > 0 && strlen(RBLastHost) > 0)
					sprintf(nil "%s (%s)  " RBLastIP RBLastHost))
				((strlen(RBLastIP) > 0)
					sprintf(nil "%s  " RBLastIP))
				((strlen(RBLastHost) > 0)
					sprintf(nil "%s  " RBLastHost))
				(t ""))
			RBMonitor->RBMState->value = sprintf(nil
				"RAMIC Bridge: Running   %sipc=%L  pid=%L  bind=%s"
				hostStr RBIpc RBLastPid bind)
		)
	else
		RBMonitor->RBMState->value = if(RBLastPid
			sprintf(nil "RAMIC Bridge: Down   (last %s pid=%L port=%d)"
				cond(
					((strlen(RBLastIP) > 0 && strlen(RBLastHost) > 0)
						sprintf(nil "%s (%s)" RBLastIP RBLastHost))
					((strlen(RBLastIP) > 0) RBLastIP)
					((strlen(RBLastHost) > 0) RBLastHost)
					(t "host=?"))
				RBLastPid RBPort)
			"RAMIC Bridge: Down")
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

procedure(RBPrintEmptyLines(@optional (n 10))
    ; Push old CIW lines off-screen for a clean visual workspace.
    ; Default 10; pass any positive integer.
    for(i 1 n printf("\n"))
)

; Auto-start the bridge when this file is loaded (no second daemon if already up)
RBStart()
