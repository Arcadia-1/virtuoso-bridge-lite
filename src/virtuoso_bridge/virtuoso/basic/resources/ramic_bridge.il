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

; Daemon-reported runtime stats (counter / errors / uptime in seconds).
; Updated by RBIpcErrHandler when the daemon emits a [RB-stat] line.
unless(boundp('RBStatCount) RBStatCount = 0)
unless(boundp('RBStatErrors) RBStatErrors = 0)
unless(boundp('RBStatUptime) RBStatUptime = 0)

procedure(RBIpcDataHandler(ipcId data)
  ; Handler for successful data received from the Python daemon.
  ; Code is executed via evalstring(), which bypasses CIW's interactive loop.
  ; As a result, CIW output (printf) is line-buffered: only text ending with
  ; "\n" is flushed immediately.  Without "\n", output stays in the buffer
  ; until the next newline or explicit flush.  This differs from typing
  ; directly in CIW, where the interactive loop flushes after every command.
  ;
  ; We wrap data in (progn ...) so multi-statement payloads run all the
  ; way through.  Bare evalstring() parses ONE form and silently drops
  ; the rest -- callers who paste multi-statement SKILL would see only
  ; the first form take effect.  The progn wrap is a no-op for
  ; single-form input and returns the last form's value for multi-form
  ; input, matching what every Python caller already expects.
  let((result)
    when(RBEcho printf("[RAMIC Bridge (%L)] receive:%L\n" ipcId data))
    if(errset(result=evalstring(strcat("(progn " data ")"))) then
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
    let((sep saw_banner saw_stat prevPid)
        sep = strcat(" "
                     buildString(list(intToChar(9)))
                     buildString(list(intToChar(10))))
        prevPid = RBLastPid
        saw_banner = member("[RB-banner]" parseString(data sep))
        saw_stat = member("[RB-stat]" parseString(data sep))
        ; Parse [RB-stat] count=N errors=K uptime=Ts (latest one wins;
        ; the daemon emits these throttled to ~1 Hz).
        when(saw_stat
            foreach(tok parseString(data sep)
                when(strlen(tok) > 6 && equal(substring(tok 1 6) "count=")
                    RBStatCount = atoi(substring(tok 7)))
                when(strlen(tok) > 7 && equal(substring(tok 1 7) "errors=")
                    RBStatErrors = atoi(substring(tok 8)))
                when(strlen(tok) > 7 && equal(substring(tok 1 7) "uptime=")
                    RBStatUptime = atoi(substring(tok 8))))
            when(boundp('RBMonInstalled) errset(RBMRefresh())))
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
        RBLastHost = ""
        RBLastIP = ""
        RBStatCount = 0
        RBStatErrors = 0
        RBStatUptime = 0)

    if(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) then
        printf("[RAMIC Bridge ipc=%L pid=%L running_bind=%s] already running; load() does not replace a running daemon; current config port=%d\n"
            RBIpc RBLastPid
            if(RBLastBind && strlen(RBLastBind) > 0 RBLastBind "unknown")
            RBPort)
        printf("[RAMIC Bridge] To switch profile/port: run RBStop() or RBStopAll(), then load setup again.\n")
    else
        ; Clear leftover banner state so the new daemon's banner is
        ; recognised as the first-of-its-lifetime by RBIpcErrHandler.
        RBLastPid = nil
        RBLastBind = ""
        RBLastHost = ""
        RBLastIP = ""
        RBStatCount = 0
        RBStatErrors = 0
        RBStatUptime = 0
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
    RBLastIP = ""
    RBStatCount = 0
    RBStatErrors = 0
    RBStatUptime = 0
    RBStderrBuf = ""
    printf("[RAMIC Bridge] RBStopAll: killed own daemons (UID-scoped, port=%d); verify: ss -tlnp | grep %d\n"
        RBPort RBPort)
)

; ============================================================================
; GUI Components for the RAMIC Bridge Monitor
; ============================================================================
; Re-running hiCreateAppForm with the same ?name *does* work on this IC build
; (it replaces the existing form), so layout edits applied via reload() take
; effect by setting RBMonInstalled = nil before reloading.  hiInsertBannerMenu
; isn't as friendly -- repeating it stacks the menu in the banner -- so the
; menu portion is gated separately further below.
;
; Guard checks the *value* (with a boundp check on the side, so a fresh
; SKILL session whose RBMonInstalled is unbound doesn't error on the value
; read).  ``RBMonInstalled = nil`` on its own is enough to force a rebuild
; on the next reload; nothing here needs SKILL's missing makunbound/unset.
unless(boundp('RBMonInstalled) && RBMonInstalled
progn(
; ----------------------------------------------------------------------
; Status section -- four stacked labels.  Section header sits on its
; own row; the four data labels are populated by RBMRefresh().  Old
; single-RBMState label is kept for back-compat (other code may set it
; out of habit) but no longer shown in the layout.
; ----------------------------------------------------------------------
; Legacy single-line label kept for back-compat with code that still
; sets RBMonitor->RBMState->value.  Not laid out in the new form.
; Empty labelText would error out on hiCreateLabel ("required parameter"),
; so seed with one space.
RBMState = hiCreateLabel(?name 'RBMState ?labelText " " ?justification CDS_JUSTIFY_LEFT)

RBMSecStatus = hiCreateLabel(
    ?name 'RBMSecStatus
    ?labelText "STATUS"
    ?justification CDS_JUSTIFY_LEFT)

RBMStatusIcon = hiCreateLabel(
    ?name 'RBMStatusIcon
    ?labelText "(unknown)"
    ?justification CDS_JUSTIFY_LEFT)

RBMHostLabel = hiCreateLabel(
    ?name 'RBMHostLabel
    ?labelText " "
    ?justification CDS_JUSTIFY_LEFT)

RBMConnLabel = hiCreateLabel(
    ?name 'RBMConnLabel
    ?labelText " "
    ?justification CDS_JUSTIFY_LEFT)

RBMStatsLabel = hiCreateLabel(
    ?name 'RBMStatsLabel
    ?labelText " "
    ?justification CDS_JUSTIFY_LEFT)

; ----------------------------------------------------------------------
; Control section
; ----------------------------------------------------------------------
RBMSecControl = hiCreateLabel(
    ?name 'RBMSecControl
    ?labelText "CONTROL"
    ?justification CDS_JUSTIFY_LEFT)

RBMBtnRefresh = hiCreateButton(
    ?name 'RBMBtnRefresh
    ?buttonText "Refresh"
    ?callback "RBMRefresh()")

RBMBtnStart = hiCreateButton(
    ?name 'RBMBtnStart
    ?buttonText "Start"
    ?callback "RBStart() RBMRefresh()")

RBMBtnStop = hiCreateButton(
    ?name 'RBMBtnStop
    ?buttonText "Stop"
    ?callback "RBStop() RBMRefresh()")

RBMIntPort = hiCreateIntField(
    ?name 'RBMIntPort
    ?prompt "Port"
    ?value RBPort
    ?defValue RBPort
    ?callback nil)

RBMBolLocal = hiCreateBooleanButton(
    ?name 'RBMBolLocal
    ?buttonText "Local connect only (127.0.0.1)"
    ?value RBLocal
    ?defValue RBLocal
    ?callback nil)

; ----------------------------------------------------------------------
; Options section -- three boolean toggles, one per row.
; ----------------------------------------------------------------------
RBMSecOptions = hiCreateLabel(
    ?name 'RBMSecOptions
    ?labelText "OPTIONS"
    ?justification CDS_JUSTIFY_LEFT)

RBMBolEcho = hiCreateBooleanButton(
    ?name 'RBMBolEcho
    ?buttonText "Echo bridging string"
    ?value RBEcho
    ?defValue RBEcho
    ?callback nil)

RBMBolLog = hiCreateBooleanButton(
    ?name 'RBMBolLog
    ?buttonText "Daemon log (/tmp/RB.log)"
    ?value RBDLog
    ?defValue RBDLog
    ?callback nil)

; ----------------------------------------------------------------------
; Apply -- commits the form's checkbox / port edits back to SKILL
; globals (RBLocal / RBPort / RBDLog / RBEcho).  When Local / Port /
; Daemon-log changed, RBMApply() also restarts the daemon so the new
; bind setting takes effect.  Lives inline in the form (rather than as
; the Motif bottom 'Apply button) because this IC's hiCreateAppForm
; only accepts a fixed enum for ?buttonLayout, and 'Apply isn't in it.
; ----------------------------------------------------------------------
RBMBtnApply = hiCreateButton(
    ?name 'RBMBtnApply
    ?buttonText "Apply changes  (restarts daemon when port / mode / log toggles)"
    ?callback "RBMApply() RBMRefresh()")

; ----------------------------------------------------------------------
; Emergency action -- UID-scoped (only kills *this user's* daemons).
; ----------------------------------------------------------------------
RBMBtnStopAll = hiCreateButton(
    ?name 'RBMBtnStopAll
    ?buttonText "Kill MY daemons  (force, UID-scoped)"
    ?callback "RBStopAll() RBMRefresh()")

; Form geometry: each section is roughly y = [section_y .. section_y+section_h].
;   Status :  y=10..150   (header + 4 data lines)
;   Control:  y=155..255  (header + buttons row + port row)
;   Options:  y=265..380  (header + 3 checkboxes + Apply button)
;   Action :  y=435..475  (Kill button)
; Width 720 fits "<ip> (<host>) ipc=... pid=... bind=...:..." comfortably.
;
; Note: form geometry is captured at first hiCreateAppForm call.  Reloading
; ramic_bridge.il updates procedure bodies but not the live form -- close
; the form and toggle RBMonInstalled=nil before reload to pick up layout
; changes (or restart Virtuoso).  Apply lives as an inline button rather
; than ?buttonLayout 'Apply because that symbol isn't in this IC build's
; valid enum (only 'Empty/'OK/'Close/'OKCancel/'OKCancelApply/...).
hiCreateAppForm(
    ?name 'RBMonitor
    ?formTitle "RAMIC Bridge Monitor"
    ?fields list(
        ; --- STATUS ---
        list(RBMSecStatus    20:10  680:20)
        list(RBMStatusIcon   30:35  680:24)
        list(RBMHostLabel    30:60  680:20)
        list(RBMConnLabel    30:85  680:20)
        list(RBMStatsLabel   30:110 680:20)

        ; --- CONTROL ---
        list(RBMSecControl   20:155 680:20)
        list(RBMBtnRefresh   30:180 110:28)
        list(RBMBtnStart    150:180 110:28)
        list(RBMBtnStop     270:180 110:28)
        list(RBMIntPort      30:220 200:28 50)

        ; --- OPTIONS (three checkboxes + inline Apply, left-aligned) ---
        list(RBMSecOptions   20:265 680:20)
        list(RBMBolLocal     30:290 660:28)
        list(RBMBolEcho      30:320 660:28)
        list(RBMBolLog       30:350 660:28)
        list(RBMBtnApply     30:385 660:32)

        ; --- ACTION ---
        list(RBMBtnStopAll   30:435 660:32)
    )
    ; ``Empty`` drops the default OK/Cancel/Help row (~50 px).  Apply
    ; appears as the inline RBMBtnApply button above; pressing it
    ; calls RBMApply() which diffs form values vs live RBLocal/RBPort/
    ; RBDLog and restarts the daemon if any toggled.  ``?callback``
    ; is also wired to RBMApply so future layout changes that re-add
    ; an OK button keep the same semantics.
    ?buttonLayout 'Empty
    ?help ""
    ?initialSize  720:520
    ?minSize      720:520
    ?maxSize      720:520
    ?mapCB        "RBMRefresh()"
    ?callback     "RBMApply()"
)

; ============================================================================
; Menu Integration
; ============================================================================

RBMonInstalled = t
)
) ; end unless(boundp 'RBMonInstalled) + progn

; ---------------------------------------------------------------------
; Banner menu -- guarded SEPARATELY from the form because hiInsertBanner
; Menu has no remove-and-replace primitive on this IC build.  We need
; the form to be re-creatable (toggle RBMonInstalled = nil + reload),
; but the menu must stay built once per Virtuoso session.
; ---------------------------------------------------------------------
unless(boundp('RBMenuInstalled) && RBMenuInstalled
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
                ?name 'RAMIC_PrintLines10
                ?itemText "Print 10 Empty Lines"
                ?callback "RBPrintEmptyLines(10)"
            )
            hiCreateMenuItem(
                ?name 'RAMIC_PrintLines20
                ?itemText "Print 20 Empty Lines"
                ?callback "RBPrintEmptyLines(20)"
            )
        )
    )
    hiInsertBannerMenu(window(1) ramicMenu 3)
    RBMenuInstalled = t
)

procedure(RBMRefresh()
    ; Re-read SKILL state and repaint the monitor form.
    ; - State label gets ipc handle + os pid + bind:port so debugging
    ;   doesn't require a separate `ss -tlnp` round-trip.
    ; - Other widgets just mirror the current SKILL globals.
    when(boundp('RBMonInstalled)
        let((alive bind hostStr statusText newForm)
            alive = (boundp('RBIpc) && ipcIsAliveProcess(RBIpc))
            ; Bind: prefer captured (RBLastBind), fall back to derived
            bind = if(RBLastBind && strlen(RBLastBind) > 0
                RBLastBind
                sprintf(nil "%s:%d"
                    if(RBLocal "127.0.0.1" "0.0.0.0")
                    RBPort))
            ; Identity: "IP (hostname)" if both, else whichever
            hostStr = cond(
                ((strlen(RBLastIP) > 0 && strlen(RBLastHost) > 0)
                    sprintf(nil "%s (%s)" RBLastIP RBLastHost))
                ((strlen(RBLastIP) > 0)   RBLastIP)
                ((strlen(RBLastHost) > 0) RBLastHost)
                (t ""))
            ; Status indicator -- text only.  ?foreground colour writes
            ; on Motif labels are silently ignored on this IC build,
            ; so we use unicode glyphs for the visual cue instead.
            statusText = cond(
                (alive && RBLastPid              "[OK]      Running")
                (alive                           "[...]     Starting / Suspect")
                (t                               "[--]      Down"))

            ; --- New-style form (post-restart): RBMStatusIcon exists. ---
            ; --- Old-style form (legacy): only RBMState exists.       ---
            newForm = car(errset(RBMonitor->RBMStatusIcon))
            if(newForm
                progn(
                    errset(RBMonitor->RBMStatusIcon->value = statusText)
                    errset(RBMonitor->RBMHostLabel->value =
                        if(strlen(hostStr) > 0
                            sprintf(nil "Host:  %s" hostStr)
                            "Host:  (banner not yet received)"))
                    errset(RBMonitor->RBMConnLabel->value =
                        if(alive
                            sprintf(nil "ipc=%L     pid=%L     bind=%s"
                                RBIpc RBLastPid bind)
                            if(RBLastPid
                                sprintf(nil "(last)  pid=%L  port=%d"
                                    RBLastPid RBPort)
                                " ")))
                    errset(RBMonitor->RBMStatsLabel->value =
                        if(alive
                            sprintf(nil "%d calls   %d errors   up %s"
                                RBStatCount RBStatErrors
                                RBFormatUptime(RBStatUptime))
                            " "))
                )
                ; Old form path -- pack everything into a single line
                errset(RBMonitor->RBMState->value =
                    if(alive
                        sprintf(nil
                            "RAMIC Bridge: Running   %s   ipc=%L  pid=%L  bind=%s   |   %d calls (%d err)  up %s"
                            hostStr RBIpc RBLastPid bind
                            RBStatCount RBStatErrors
                            RBFormatUptime(RBStatUptime))
                        if(RBLastPid
                            sprintf(nil "RAMIC Bridge: Down   (last %s pid=%L port=%d)"
                                if(strlen(hostStr) > 0 hostStr "host=?")
                                RBLastPid RBPort)
                            "RAMIC Bridge: Down")))
            )

            errset(RBMonitor->RBMIntPort->value = RBPort)
            errset(RBMonitor->RBMBolLocal->value = RBLocal)
            errset(RBMonitor->RBMBolEcho->value = RBEcho)
            errset(RBMonitor->RBMBolLog->value = RBDLog)
        )
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

procedure(RBFormatUptime(s)
    ; Render a seconds-int as compact "<H>h <M>m" / "<M>m <S>s" / "<S>s".
    ; Avoids quotient / mod / fix; only ``truncate`` (towards zero) +
    ; subtraction, which are stable across all observed IC builds.
    let((sec h m)
        sec = s
        h = truncate(sec / 3600.0)
        sec = sec - h * 3600
        m = truncate(sec / 60.0)
        sec = sec - m * 60
        cond(
            ((h > 0) sprintf(nil "%dh %dm" h m))
            ((m > 0) sprintf(nil "%dm %ds" m sec))
            (t sprintf(nil "%ds" sec))))
)

procedure(RBPrintEmptyLines(@optional (n 10))
    ; Push old CIW lines off-screen for a clean visual workspace.
    ; Default 10; pass any positive integer.
    for(i 1 n printf("\n"))
)

; Auto-start the bridge when this file is loaded (no second daemon if already up)
RBStart()
