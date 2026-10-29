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

The stdlib-only harness below runs real temporary daemons, sockets and kernel
locks without Cadence, a PDK, SSH, pytest or existing credentials:

```bash
python3 tests/native_instance_registry.py src/virtuoso_bridge/virtuoso/basic/resources
```

To exercise the same allocation, credential and real-daemon protocol checks
with Python 2.7 workers, keep the harness on Python 3 and select the interpreter:

```bash
VB_TEST_WORKER_PYTHON=/path/to/python2.7 python3 tests/native_instance_registry.py src/virtuoso_bridge/virtuoso/basic/resources
```

It covers twelve concurrent ephemeral allocations, fixed-port races and
conflicts, request flags and private permissions, SIGKILL/discovery cleanup,
graceful cleanup, nonblocking liveness checks, symlinks/FIFOs, concurrent
legacy credential creation and overrides, directory replacement, signed instance identity,
rejection of invalid/foreign credentials, and absence of secrets in output. All files
are synthetic and temporary. It does not switch Linux UIDs or test native
SKILL timers, a live CIW, or electrical equivalence. The related pytest cases
skip Linux-only operations on Windows.

Linux CI runs the harness on both supported Python matrix lanes. Its Python
3.13 lane builds CPython 2.7.18 from the pinned python.org source and verified
SHA256, installs it under runner temporary storage, then selects that actual
worker with `VB_TEST_WORKER_PYTHON`. The harness itself stays on Python 3.
Non-Linux invocation exits with an explicit skip before creating fixtures.

Both Linux matrix jobs (Python 3.9 and 3.13), including actual Python 2.7 worker
execution on the 3.13 lane, are required acceptance evidence for instance
discovery. Windows cannot establish registry lock liveness, POSIX ownership,
`/proc` identity or shell behavior. Obtain a green run on the current hosted
runner image before merge; a successful local build is separate evidence.

`test_generated_bundle.py` exercises real Linux locked publication, competing
publishers, refusal before replacement, staging rollback and interrupted-exchange
recovery. `test_discovery_round2.py` covers CLI output/routing, local screenshot
paths with real file copies, build parity, private state reads and restart request
ordering. Its Linux registered-entrypoint test uses real temporary locked records.
Mocked SKILL image output and restart replies are not native Cadence evidence.

`test_discovery_compatibility.py` covers discovery-first use of an old configured
port, automatic adoption with unchanged configuration, real loopback signed
hello/authentication refusal, lazy Python connection, bounded restart
reconciliation without replay, and the removal of scratch-resolution deadlines. Its native POSIX
metadata case is skipped on Windows. SSH stopping uses the original saved-PID
behavior; executable/birth/argv verification is not part of this revision.

`test_remote_deploy_dirs.py` executes bounded POSIX shells against real private,
permissive and symlink fixtures for the original deployment checks.
`test_generated_bundle.py` also covers unexpected inventory,
clean replacement, backup bookkeeping and cleanup errors. The native registry
harness checks that read-only discovery leaves stale files intact; daemon-side
cleanup is tested separately. Windows skips these Linux cases. The generated-bundle
suite is included in the Windows transport job; the full Linux jobs execute
the native cases. A configured CI job is not evidence that it has passed.

`VB_TEST_DAEMON_RESOURCES=/absolute/resources` selects alternate daemon resources
for source checks that support it; `test_auto_port_state.py` reports the resolved
path as the `daemon_resources` test property. Those checks only inspect source.
Do not present them as SKILL execution or silently test an installed old bundle.
The native harness takes its resource path explicitly and exercises startup,
publication, active-writer protection, stale cleanup and authentication.

Use the project virtualenv and registered `virtuoso-bridge` entrypoint for CLI
subprocess tests. Keep live `.env` settings out of offline test runs. Linux shell
tests must actually execute a bounded shell, including successful private staging
and failure cases; Windows/source-only assertions do not replace these checks.
Report Windows, native Linux, Python 2.7 and Cadence results separately.

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
stop. It uses the existing account credential in automatic mode and never loads
code into existing user CIWs. This verifies native SKILL behavior on the installed
Cadence build; it is not cross-version or electrical-equivalence evidence.

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
