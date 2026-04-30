; Minimal Virtuoso SKILL bridge — load this once in CIW:
;   load("/path/to/langgraph/my_bridge.il")
;
; Before starting Virtuoso, set in your shell:
;   export MY_BRIDGE_DIR=/path/to/langgraph

DAEMON_PORT = 12345
DAEMON_PATH = strcat(getShellEnvVar("MY_BRIDGE_DIR") "/my_daemon.py")

myIpc = ipcBeginProcess(
    sprintf(nil "python3 %s 127.0.0.1 %d" DAEMON_PATH DAEMON_PORT)
    "" 'myDataHandler nil nil "")

procedure(myDataHandler(ipcId data)
    let((result)
        if(errset(result = evalstring(data)) then
            ipcWriteProcess(ipcId sprintf(nil "%c%L%c" intToChar(2) result intToChar(30)))
        else
            ipcWriteProcess(ipcId sprintf(nil "%c%s%c" intToChar(21) errset.errset intToChar(30)))
        )
    )
)

printf("[my_bridge] daemon: %s  port: %d\n" DAEMON_PATH DAEMON_PORT)
