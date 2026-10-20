# Optional MCP adapter

The stdio server exposes the existing Virtuoso and Spectre Python APIs to MCP clients.
It uses the same bridge environment and local/SSH configuration as the CLI.

## Install

From the repository root:

```sh
uv venv
uv pip install -e '.[dev]'
.venv/bin/python mcp_server.py
```

On Windows use `.venv/Scripts/python.exe`. Copy `mcp.example.json` into your
client configuration and replace the interpreter and server paths with absolute
paths for this checkout. Send logs to stderr; stdout is JSON-RPC only.
For waveform images with axis labels and a legend, install the optional visual
extra with `uv pip install -e '.[mcp-visuals]'`. Without it, the PNG and its
numeric axis/legend description still appear in the tool result.

## Operations

Tools cover bridge status/setup, raw SKILL, library/cell inspection, PDK
inspection, schematic editing, Maestro, standalone Spectre, waveform reading,
screenshots and existing documentation/SKILL lookup APIs. `search_docs` delegates
to the bridge's local/remote documentation search. `read_waveform` uses the
existing PSF parser, with nearest timestamp selection (ties choose the earlier sample).
`screenshot` returns an MCP PNG image block and its saved file path in the same
tool result. `simulate` and `read_waveform` return a PNG waveform preview with
the color legend, real time range, numeric stats and source path alongside it.
The preview keeps per-pixel extrema so a brief event is not silently discarded.
Clients decide whether to display image blocks inline; the saved file and
structured values remain available when a client collapses tool output.

Use `check_schematic` for scoped `schCheck`/`dbSave` counts and diagnostics,
`screenshot` for visual context, and Spectre/PSF results for electrical data.
An image or a successful tool call alone does not establish electrical signoff.
If a check or Maestro run has an unknown outcome, retain the returned request
metadata, history and completion marker before any retry.

`list_pdk`, `search_pdk` and `describe_pdk` inspect this MCP server's local
filesystem. They do not enumerate remote-only libraries. `list_libraries`
queries the active Virtuoso session.

`startup` invokes the registered `virtuoso-bridge start` command and returns its
CIW load instructions. Setup completion does not establish live readiness.
No automatic launch, lock deletion or process termination is performed.

`new_schematic` and `build_schematic` require explicit `replace=true` to replace
an existing view. Use `fresh=false` to modify through the existing schematic
builder. Maestro `run_test` requires a session; `test_results` requires both a
session and an acknowledged history. A run is read only after confirmed completion.

SKILL operations are never automatically replayed after a timeout. The client
and original request diagnostics are retained for reconciliation. Use the
bridge's documented recovery API before deciding whether a new request is safe.

Unified search, the optional fast parser and signoff previews are separate
contributions. This adapter neither imports nor requires those implementations.

## Protocol and verification

Supports modern `server/discover` (2026-07-28) and legacy `initialize`
(2025-11-25, 2024-11-05). Tools run synchronously with bounded timeouts.
No independent task/recovery engine is advertised. Existing repository skills
are exposed through the skills extension and resources.

```sh
uv run --extra dev pytest tests/test_mcp_adapter.py
PYTHON=.venv/bin/python bash mcp/mcp_smoke.sh
```

These are offline checks; they do not establish Cadence/PDK electrical correctness.
