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

The default run needs only plain Virtuoso and Spectre. Optional variables
enable more checks:

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
