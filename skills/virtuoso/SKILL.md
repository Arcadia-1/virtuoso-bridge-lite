---
name: virtuoso
description: "Operate Cadence Virtuoso through virtuoso-bridge: SKILL, CIW, schematic/symbol/layout, GDS, and ADE Maestro. Use spectre for standalone netlist-driven simulation."
---

# Virtuoso

Use the existing bridge for live Cadence operations. Python runs on the client;
SKILL runs in the selected Virtuoso process. Local mode needs no SSH.
Spectre execution is independent of this connection.

## Choose the smallest interface

- One-off SKILL: `virtuoso-bridge eval 'EXPR'`, `eval --stdin`, or `load FILE.il`.
- Common structured operations: `client.schematic`, `client.symbol`,
  `client.layout`, `client.library` and `client.maestro`.
- Uncovered operations: `client.execute_skill(...)` or `client.load_il(...)`.
  Raw SKILL is a supported escape hatch; a new wrapper is not a prerequisite.

Keep one-host use simple. Multiple servers/accounts/processes are optional;
do not require profile inventory or process metadata for ordinary work.
For GUI-equivalent simulation, preserve the netlist, models, corner and solver
options and refresh the applicable PDK/CDF callbacks.

## Before a live operation

Use the existing project virtual environment and configuration. If the bridge
is not connected or the target changes, read [connection.md](references/connection.md).
Pure documentation work and offline tests do not require a live connection.

Verify SKILL function signatures against the target's installed Cadence docs
and inspect actual library cells, symbol pins and CDF parameters before use.
Read [local-docs.md](references/local-docs.md) for the verification workflow.
A miss in repository examples is not proof that a function does not exist.
Check Python signatures against the active package, and confirm availability
of version-specific commands with `virtuoso-bridge --help`.

When an installed skill is a symlink, resolve its directory before following
repository-relative paths. References describe current supported behavior;
an older client or daemon may lack the documented capabilities.

## Editing and callback correctness

Use `create()` for a new or explicitly authorized replacement cellview and
`modify()` for an existing design. Preserve unrelated instances and user edits.
Read back connectivity and parameters, then check/save before netlisting.
Creation is not permission to delete an existing cell first.

For PDK devices, use the parameter/callback helpers where they support the
actual device; otherwise verify and invoke the required callbacks through SKILL.
Editable names and derived fields are PDK-specific: `nf` and `fingers` are not
universally interchangeable. Details:
[schematic-skill-api.md](references/schematic-skill-api.md).

## Shared CIW and uncertain outcomes

Before popup-prone operations or timeout recovery, read
[shared-ciw-dialogs.md](references/shared-ciw-dialogs.md).
It is the authority for dialog ownership, opt-in guards, request receipts and
explicitly reviewed informational close.

Inspect through SSH/X11 when SKILL is blocked. Leave unknown-origin dialogs to
the user; never automatically press Enter/Cancel or close the current form.
Preserve the original request handle/history after uncertain execution.
Do not resubmit, purge sessions or restart a pending daemon to unblock a wait.
On old clients without the documented protection, use manual coordination
rather than assuming that newer instructions make legacy execution safe.

Maestro lifecycle helpers can close/purge sessions. Use them only within the
authorized lifecycle scope; on a shared CIW resolve the exact existing session.
For results, bind reads/exports to the acknowledged history and use unique
artifact paths. A sent key or nonempty output is not proof of a successful save/run.

## Task references

Read the branch needed for the operation; do not load the entire catalog.

| Operation | Reference |
| --- | --- |
| CLI/Python calls, file transfer, batched attribute reads, CIW output | [client-basics.md](references/client-basics.md) |
| Schematic editing, readback and CDF callbacks | [schematic-python-api.md](references/schematic-python-api.md), [schematic-skill-api.md](references/schematic-skill-api.md) |
| Recreate a schematic from an existing design | [schematic-recreation.md](references/schematic-recreation.md) |
| Import an exact-coordinate schematic manifest | [schematic-manifest-import.md](references/schematic-manifest-import.md) |
| Symbol generation/editing | [symbol-python-api.md](references/symbol-python-api.md) |
| Layout geometry, routing, vias and GDS operations | [layout-python-api.md](references/layout-python-api.md), [layout-skill-api.md](references/layout-skill-api.md) |
| Libraries and technology binding | [library-python-api.md](references/library-python-api.md) |
| SOS checkout/checkin of one explicitly selected cellview | [sos-python-api.md](references/sos-python-api.md) |
| Maestro configuration, snapshots and result reading | [maestro-python-api.md](references/maestro-python-api.md), [maestro-skill-api.md](references/maestro-skill-api.md) |
| GUI-based simulation lifecycle | [simulation-flow.md](references/simulation-flow.md) |
| Netlist import/export or batch netlisting without Maestro | [netlist.md](references/netlist.md), [batch-netlist-si.md](references/batch-netlist-si.md) |
| Cellview files, stale readback after another session saves, lock files and text/binary boundaries | [cellview-on-disk-layout.md](references/cellview-on-disk-layout.md) |
| SKILL Finder API | [skill-finder-python-api.md](references/skill-finder-python-api.md) |
| Errors, stalled batch tools or connection problems | [troubleshooting.md](references/troubleshooting.md) |

When working in the repository, locate relevant examples with
`rg --files examples/01_virtuoso`. Use the corresponding API reference rather
than maintaining a second tutorial in this entrypoint.
