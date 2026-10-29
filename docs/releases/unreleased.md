# Unreleased instance discovery

New bootstraps bind an OS-assigned port and publish locked instance records.
Clients discover sessions by account and optional `VB_REMOTE_WORK_DIR`.
`sessions [--json]` lists live instances; `start --port PORT` selects an exact
remote port when several sessions match.

Discovery prefers a matching `VB_REMOTE_PORT` record, then a unique registered
instance. Successful empty discovery can use the configured fixed-port endpoint;
discovery errors, ambiguity and authentication failures never trigger fallback.
See the [design](../adr/0004-instance-discovery.md).

`status --port PORT` checks an exact instance. `status --inspect-state` displays
retained local state without contacting a daemon; `stop --port PORT` targets the
saved remote endpoint. Restart preserves the selected CIW and authenticates its
replacement without replaying an uncertain request. `RBStopAll()` now stops only
that CIW's tracked child.

Generated bootstraps use `/tmp/virtuoso_bridge_<user>/daemon`, with records under
`runs/`. `start --replace-setup` explicitly replaces a differing generated bundle
and retains its predecessor. `VB_DAEMON_SETUP` selects a versioned shared
installation, which clients never overwrite. Running CIWs need an explicit load
to use newly staged code.

Local screenshots now create and expand their destination directory. Session
listings preserve printable Unicode while escaping terminal controls.

See the [connection guide](../../skills/virtuoso/references/connection.md) for
usage and [test instructions](../../tests/README.md) for offline and live checks.
