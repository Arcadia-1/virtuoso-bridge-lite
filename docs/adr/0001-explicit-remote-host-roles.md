# Keep explicit remote host roles behind a legacy fallback

Virtuoso installations can place the GUI, `ipcBeginProcess()` daemon, bridge files, and Spectre on different machines, so the bridge resolves explicit GUI, daemon, deployment, and Spectre hosts rather than treating one SSH target as physical truth. `VB_REMOTE_HOST` remains the fallback for every role to preserve existing one-host configurations; the trade-off is a compatibility alias in the model, while split deployments gain an unambiguous tunnel endpoint and a deployment root that can be validated across hosts.

## Consequences

Long-lived port forwards own a standalone OpenSSH process and never attach to the command-session ControlMaster. A global jump host is ignored when it is identical to a role target, preventing GUI-host self-jumps.

