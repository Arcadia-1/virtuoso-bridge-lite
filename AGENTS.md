# Agent guide — virtuoso-bridge-lite

Control Virtuoso through Python/SKILL and run Spectre independently.

## Lite boundary

Spectre execution and PDK/CDF callbacks are the core. Lightweight schematic,
symbol and GDS operations cover common work; raw SKILL handles the long tail.
Existing one-host configuration and APIs remain sufficient. Multi-server,
multi-account and process configuration are opt-in, with no mandatory migration.
Before adding configuration or workflow abstractions, read
[the Lite decision](docs/adr/0003-lite-default-optional-advanced-config.md).

## Working in this repository

- Use `uv` and a project virtual environment. Install with `uv pip install -e .`.
- Canonical implementation: `src/virtuoso_bridge/`; runnable workflows:
  `examples/`; standalone helpers: `tools/`.
- Locate modules with `rg --files` before guessing paths. CLI entrypoint:
  `virtuoso_bridge.cli:main`, registered as `virtuoso-bridge` in `pyproject.toml`.
  `python -m virtuoso_bridge.cli` is not the console entrypoint.
- Preserve unrelated working-tree changes. Prefer an existing clean worktree
  for work that overlaps them.
- Documentation edits and offline tests need no SSH, daemon startup or GUI
  interaction. Connect only when the task actually needs live EDA operations.
- Installed skills may be symlinks to another checkout. Resolve their paths
  before editing; check the active client and daemon capabilities before using
  version-specific APIs. Updating instructions does not upgrade running code.

## Operational boundaries

Execute SKILL through `VirtuosoClient` or `virtuoso-bridge eval/load`.
Use explicit schematic/layout `create()` or `modify()`; replacing an existing
design requires authorization. Check/save and refresh applicable PDK callbacks
before netlisting; raw property changes alone do not reproduce GUI behavior.

Treat a shared CIW as user-owned. Inspect suspected dialogs out of band; let the
user resolve unknown-origin dialogs. An uncertain request is not a failed
request: preserve its handle/history and reconcile the original outcome before
any retry. Read [shared-CIW protection](skills/virtuoso/references/shared-ciw-dialogs.md)
before popup-prone operations or recovery; its guard is opt-in and version-gated.
Keep authentication and daemon identity checks enabled on shared hosts.

## Read only the relevant references

- Live Virtuoso/SKILL, schematic, symbol, layout, GDS or Maestro:
  [virtuoso skill](skills/virtuoso/SKILL.md).
- Standalone netlist-driven simulation and PSF:
  [spectre skill](skills/spectre/SKILL.md). No Virtuoso daemon is required.
- Netlist cleaning: [netlist skill](skills/netlist/SKILL.md).
- Parameter optimization: [optimizer skill](skills/optimizer/SKILL.md).
- First connection, host/configuration changes or virtualenv rebuilding:
  [connection guide](skills/virtuoso/references/connection.md).
- SKILL functions, PDK parameters or version-dependent behavior:
  [installed-doc verification](skills/virtuoso/references/local-docs.md).
- Issues/PRs: use `gh`, then read [tracker guidance](docs/agents/issue-tracker.md).
  For issue triage also read [label mapping](docs/agents/triage-labels.md).
- Domain/interface design or review: [domain decisions](docs/agents/domain.md).
- Explicit profile auditing or site environment selectors:
  [profile audit](docs/profile-audit.md); these are not default setup steps.
- Requested traffic maintenance: [traffic guide](docs/agents/traffic.md).
- Windows skill links cloned as text: inspect `scripts/fix-symlinks.sh` before
  running it for the affected checkout. General user setup is in [README](README.md).

## Verification

Run proportionate tests in the project environment:

```bash
uv run --extra dev pytest
```

Check `pyproject.toml` and `.github/workflows/tests.yml` for the current test
configuration and platform coverage. Shell/environment changes need real,
bounded shell execution, not just command-string assertions; report skipped
shell cases. CLI subprocess tests should exercise the registered entrypoint.
Documentation-only changes need valid references and skill frontmatter checks.

State separately what was tested offline and what was verified on real
Cadence/PDK hardware. A green fixture suite does not establish GUI-equivalent
electrical results. Test layers, live-test variables and the rule against
committing lab or PDK data are in [tests/README.md](tests/README.md).
