# Tests

Virtuoso, Spectre and foundry PDKs are licensed tools, so most of this suite
runs without them. Tests are organised in three layers by what they need.

| Layer | Needs | Runs where | Files |
|---|---|---|---|
| Offline | nothing | CI and every checkout | everything not listed below |
| Recorded Cadence output | nothing to run; any Virtuoso to re-record | CI and every checkout | `test_recorded_cadence_regressions.py`, `fixtures/cadence/` |
| Live | a Virtuoso session reached through the bridge | a maintainer's own machine, by hand | `test_bridge_live.py`, `test_bridge_live_gds.py`, `test_geometry_regressions_live.py` |

```bash
uv run --extra dev pytest            # offline + recorded layers
```

There is no hosted runner for the live layer. Maintainers run it on a
machine with Cadence access before a release or after changing code that
talks to Virtuoso, and report the result in the pull request.

## Recorded Cadence output

`fixtures/cadence/` holds output captured from plain Virtuoso (analogLib and
SKILL forms, no PDK): X11 properties of a blocking form and of the CIW, and
schCheck log lines. `manifest.json` names the release. Re-record against any
Virtuoso session with:

```bash
python tools/record_cadence_fixtures.py --lib <scratch lib> [--env <bridge .env>]
```

The recorder scrubs host names, user names and paths. Data that cannot be
published (anything from a foundry PDK) is modelled instead and labelled as
such where it is defined.

## Native Linux instance discovery checks

Run the stdlib-only harness with temporary daemons, sockets and kernel locks:

```bash
python3 tests/native_instance_registry.py src/virtuoso_bridge/virtuoso/basic/resources
# Optional actual Python 2.7 workers; the harness stays on Python 3:
VB_TEST_WORKER_PYTHON=/path/to/python2.7 python3 tests/native_instance_registry.py src/virtuoso_bridge/virtuoso/basic/resources
```

It covers concurrent allocation, fixed-port requests, record liveness and cleanup,
filesystem edge cases, bundle publication and signed daemon authentication.
Fixtures are synthetic; it needs no Cadence, PDK, SSH or existing credentials.
Non-Linux runs skip before creating fixtures.

`test_instance_binding.py` uses real localhost sockets and the shipped Python 3
handler with synthetic CIW responses. It checks cached/fresh clients after
same-port replacement, replacement between hello and execution, signed instance
and request fields, replay rejection, the standalone client, and legacy fallback.
It runs on Windows too; it does not require Cadence.

Run the full-daemon startup harness separately:

```bash
python3 tests/native_daemon_concurrency.py src/virtuoso_bridge/virtuoso/basic/resources
```

Workers wait at a startup barrier before importing the actual daemon. The tests
exercise first credential creation, automatic/fixed port contention, signed
execution over synthetic IPC, same-project records, and killed-worker cleanup.
By default these use the current UID; two-account cases explicitly skip.
For a disposable Linux test runner, provision two non-root fixture logins and run
the harness as root with `VB_TEST_ACCOUNT_USERS=login_a,login_b`. It drops each
worker to its real UID/GID, clears supplementary groups, and uses private temporary
homes, credentials and registries. It never creates accounts or reads their real
home directories. Two-account cases also check token access denial, cross-token
rejection and cleanup isolation. `VB_TEST_WORKER_PYTHON` selects actual Python 2.7
workers for the same cases. Linux CI provisions and removes its fixture accounts
and runs both worker versions; account/root prerequisites are not simulated.

| Required regression | Coverage |
|---|---|
| Same-account and different-account concurrent startup | `native_daemon_concurrency.py`: barrier-synchronized workers, automatic and fixed ports; different accounts require real fixture UIDs |
| Multiple instances in one project | Native startup harness plus `test_daemon_lifecycle_live.py`: shared project, independent records and surviving peer |
| Exit and abnormal leftovers | Live legacy/registered exit, kill and abort; native killed-worker stale-record exclusion and sweep |
| Wrong connection after port reuse | `test_instance_binding.py`: real sockets, cached/fresh clients, replacement between handshake and execution, same/different tokens |
| Default single-session compatibility | `test_discovery_compatibility.py` and legacy request tests; live fixed-port connection, restart and pre-discovery upgrade |
| GUI/daemon/deployment host separation | `test_remote_host_roles.py`: distinct host runners/interpreters, bundle visibility, discovery/restart routing and native GUI process identity; separate physical hosts need site acceptance |

Linux CI runs Python 3.9 and 3.13; the latter also builds pinned CPython 2.7.18
for worker checks. Obtain green Linux results before merge. Windows cannot
verify POSIX permissions, `flock` or `/proc` behavior. Offline checks do not
establish native SKILL, CIW or electrical equivalence; report live results
separately. `VB_TEST_DAEMON_RESOURCES` selects alternate resources for source
checks, while the native harness takes its resource path explicitly.

`test_pr269_review.py` covers default-off mode (including workdir-only config),
exact fixed endpoints, expired selections across CLI/Python paths, and isolated
historical state. `test_private_instance_paths.py` runs real bounded Linux shell
commands and descriptor operations for unsafe roots/ancestors and path swaps.
The two-account startup harness also rejects foreign-owned pre-created deployment
and registry paths using actual UIDs, with both Python 3 and Python 2.7 workers.

## Live tests

Use a dedicated Virtuoso session, for example one started under
`cdsXvfb-run`, never a CIW someone is working in: the tests open windows and
briefly show a blocking form. Every cell they create is named `VB_LIVE_<id>`
inside the scratch library and is removed at the end, together with what it
left on the Virtuoso host: netlist exports use a private directory, and the
Maestro checks remove their cell's run data under the simulation directory
(`<projectDir>/<lib>/<cell>`, `~/simulation` by default).

```bash
VB_RUN_LIVE_TESTS=1 VB_TEST_ENV_FILE=<bridge .env> VB_TEST_LIB=<scratch lib> \
uv run --extra dev pytest tests/test_bridge_live.py tests/test_bridge_live_gds.py -v
```

The smaller `test_geometry_regressions_live.py` suite checks offset-label
terminal connectivity in four directions, polyline vertices and mosaic arrays
using only analogLib and plain layout geometry. Run it with the same dedicated
session and scratch-library variables; it needs no Spectre or foundry PDK.

`test_layout_read_lifecycle_live.py` instead launches two isolated, headless
Virtuoso processes in private working directories. It creates its own scratch
library and plain rectangles, never connecting to an existing bridge or CIW.
It checks external-save visibility, repeated reads, borrowed/unsaved views,
and error-path cleanup for both layout readers. Each case also runs with the
generated newer lookup name replaced by an undefined test symbol, exercising
the legacy lookup in native SKILL without changing protected system functions.
This simulates a missing API; it is not a substitute for testing another IC build.
Run on a Linux Cadence host
with the project environment and a configured Cadence license environment:

```bash
VB_RUN_FRESHNESS_TESTS=1 VB_TEST_VIRTUOSO=/path/to/virtuoso \
uv run --extra dev pytest tests/test_layout_read_lifecycle_live.py -v
```

This is native OA lifecycle coverage, not PDK or electrical-equivalence testing.
The offline generation tests do not substitute for it.

The lifecycle suite also exercises CLI restart/reconnect with two CIWs in one
working directory and a shared registry. Set
`VB_TEST_LEGACY_RESOURCES=/absolute/pre-discovery/resources` to enable a full
old-config start/status/restart, then stop/wait/load/start/status transition in
a separate fixture-owned CIW. The old port stays configured when the new
registered session is adopted. Stage that
baseline bundle from the reviewed Git revision; do not replace supplied user
daemons to manufacture a migration test. Automatic/fixed-port discovery and legacy
token modes need distinct verification. Preserve any original request evidence
after uncertain execution and never retry a restart merely because its reply dropped.

Before acceptance, obtain fresh site evidence for the legacy transition,
two-CIW isolation, same-CIW restart/reload, fixed-port changes and malformed
bind banners. Exercise historical state with a fixture-owned SSH forward:
verify saved-PID stopping and inspect suspect historical processes before
signalling them. This SSH recovery check is a separate
operator acceptance step, not automated by the Cadence lifecycle suite. Supply
isolated CIWs and baseline resources as well as a reachable daemon. Missing
prerequisites must be reported as skips, not successful live verification.

The default run needs only plain Virtuoso and Spectre. Optional variables
enable more checks:

`VB_RUN_LIFECYCLE_TESTS=1 VB_TEST_VIRTUOSO=/path/to/virtuoso` enables
`tests/test_daemon_lifecycle_live.py`. Its automatic-port case launches two
private headless CIWs and checks signed build/record identity, reload, malformed
bind banners, bounded restart and timeout, monitor changes and tracked-child-only
stop. It never loads code into existing user CIWs. Exit/kill/abort cases run in both
legacy and registered modes with two CIWs sharing a project and registry, checking listener release,
read-only stale-record exclusion, explicit sweep cleanup and peer survival.
The lifecycle fixtures use private temporary credentials, including a shared
fixture credential for automatic sessions. This verifies native SKILL behavior on
the installed Cadence build; it is not cross-version or electrical-equivalence
evidence.

| Variable | Enables |
|---|---|
| `VB_TEST_LAYER`, `VB_TEST_LAYER2` | layout checks on chosen layers (default: the first two drawing layers of the scratch library's technology) |
| `VB_TEST_PDK_LIB`, `VB_TEST_NMOS_CELL` | CDF-callback check on a PDK MOS with a `fingers`/`nf` CDF |
| `VB_TEST_STREAM_MAP` | GDS export (caller-host layer map; also needs `VB_TEST_LAYER`) |
| `VB_TEST_MAESTRO=1` | Maestro sweep and durable-job checks (uses ADE licenses) |

## Known bugs as strict xfails

A reproduced but unfixed bug is written as a test of the correct behaviour
and marked `@pytest.mark.xfail(strict=True, reason="open bug: ...")`. The
suite stays green while the bug is open. The fix makes the test pass, strict
mode turns that into a failure, and the marker must be removed in the same
change, so a fix always lands together with its regression test.

## Publishing data

The repository is public. Never commit host names, user names, home
directories, license-server details or anything copied from a foundry PDK
(CDF prompts, versions, layer maps, models). Prefer re-recording from plain
Virtuoso; otherwise model the behaviour and say so.
