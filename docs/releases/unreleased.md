# Unreleased instance discovery

Automatic sessions are disabled by default. Set `VB_AUTO_SESSIONS=1` (or its
profile-suffixed equivalent) to use a registered bootstrap that binds an
OS-assigned port and publishes locked instance records. Clients discover
sessions by account and optional `VB_REMOTE_WORK_DIR`. A working directory
alone does not change the legacy default port, setup or connection sequence.
`sessions [--json]` lists live instances; `start --port PORT` selects an exact
remote port when several sessions match.

`VB_REMOTE_PORT` constrains discovery to that endpoint; an absent record can
only fall back to authenticating that same legacy endpoint. A vanished saved
identity requires explicit reselection, even within the same project. Discovery
errors, ambiguity and authentication failures never redirect the connection.
See the [design](../adr/0004-instance-discovery.md).

`status --port PORT` checks an exact instance. `status --inspect-state` displays
retained local state without contacting a daemon; `stop --port PORT` targets the
saved remote endpoint. Restart preserves the selected CIW and authenticates its
replacement without replaying an uncertain request. `RBStopAll()` now stops only
that CIW's tracked child.

In automatic mode, generated bootstraps use `/tmp/virtuoso_bridge_<user>/daemon`, with records under
`runs/`. `start --replace-setup` explicitly replaces a differing generated bundle
and retains its predecessor. `VB_DAEMON_SETUP` selects a versioned shared
installation, which clients never overwrite. Running CIWs need an explicit load
to use newly staged code.

Shared bundle installation is now packaged as
`virtuoso-bridge install-daemon DESTINATION --python /usr/bin/python3`, run on
the Linux Cadence host. This replaces `tools/install_shared_daemon.py` and
retains its protected-directory and no-overwrite checks. New registered setup
files preserve `RB_PORT` set before loading; a nonzero value overrides persistent
fixed-port requests, while zero consults those requests before OS allocation.
Existing generated files need explicit replacement or a new shared bundle to
gain this behavior. See [fixed-port setup](../../skills/virtuoso/references/connection.md#connecting-on-a-fixed-port).

Local screenshots now create and expand their destination directory. Session
listings preserve printable Unicode while escaping terminal controls.

See the [connection guide](../../skills/virtuoso/references/connection.md) for
usage and [test instructions](../../tests/README.md) for offline and live checks.

Deployment and registry roots require the current account's ownership and mode
0700; records require mode 0600. Directory ancestors are verified without
following symlinks, and publication/cleanup stay anchored to open descriptors.
Unsafe pre-existing paths are rejected without changing their permissions.
CIW birth/ownership checks run on the GUI host, independently of daemon and
deployment host roles. Offline tests isolate both current and historical state.
