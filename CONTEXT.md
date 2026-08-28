# Virtuoso Bridge Remote Topology

This context names the machines and paths that connect a local Python client to Virtuoso and Spectre. The roles may collapse onto one machine, but they are not assumed to do so.

## Language

**GUI host**:
The machine that owns the Virtuoso CIW and its X11 windows.
_Avoid_: Remote host, login host

**Daemon host**:
The machine where the RAMIC Python daemon launched by Cadence `ipcBeginProcess()` actually listens.
_Avoid_: Compute host, remote host

**Deployment host**:
The SSH target that receives generated bridge files in the deployment root.
_Avoid_: Upload host, file host

**Deployment root**:
The filesystem root containing generated bridge files; in a split topology it must be visible to the GUI and daemon hosts.
_Avoid_: Temp dir, remote scratch

**Spectre host**:
The machine that executes standalone Spectre jobs.
_Avoid_: Simulation server, remote host

**Tunnel endpoint**:
The configured daemon host and port reached by the local SSH forward.
_Avoid_: Remote host

**Legacy host**:
The `VB_REMOTE_HOST` compatibility value used as the default for every role not configured explicitly.
_Avoid_: Primary host

