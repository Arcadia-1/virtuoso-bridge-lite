; RAMIC Bridge — minimal SKILL-side IPC
; Load this in Virtuoso CIW: load("/path/to/ramic_bridge.il")

RBPort = 65432

procedure(RBIpcDataHandler(ipcId data)
  let((result)
    if(errset(result = evalstring(data)) then
      ipcWriteProcess(ipcId sprintf(nil "%c%L%c" 2 result 30))
    else
      ipcWriteProcess(ipcId sprintf(nil "%c%L%c" 21 errset.errset 30))
    )
  )
)

procedure(RBIpcFinishHandler(ipcId data)
  printf("[RAMIC] daemon exited\n")
)

unless(boundp('RBIpc) RBIpc = 'unbound)

procedure(RBStart()
  if(boundp('RBIpc) && ipcIsAliveProcess(RBIpc) then
    printf("[RAMIC] already running\n")
  else
    RBIpc = ipcBeginProcess(
      sprintf(nil "python %s %s %d"
        strcat(getShellEnvVar("RB_DAEMON_PATH") || "." "/ramic_daemon.py")
        "127.0.0.1" RBPort)
      "" 'RBIpcDataHandler nil 'RBIpcFinishHandler "")
    printf("[RAMIC] started on port %d\n" RBPort)
  )
)

RBStart()
