# MCP Connection Guide

Connect any coding agent to live Cadence in minutes.

## What this is

One MCP server. It drives Virtuoso and Spectre through tools.
Agents search, draw, simulate, and sign off. No shell needed.

## Install

You need Python 3.9 or newer. No extra packages.

```bash
git clone <this-repo> && cd <this-repo>
pip install -e .
```

The server file is `mcp_server.py`. It uses only the standard library.

## Connect opencode

Add one entry to your opencode config:

```json
{
  "mcp": {
    "servers": {
      "vb": {
        "type": "local",
        "command": ["python3", "/path/to/mcp_server.py"],
        "environment": {
          "VB_REMOTE_HOST": "localhost",
          "VB_WORK_DIR": "/path/to/agent/work",
          "VB_PDK_ROOTS": "/path/to/pdks",
          "VB_CDS_LIB": "/path/to/work/cds.lib",
          "LD_LIBRARY_PATH": "/path/to/cadence/INVS152/tools.lnx86/inca/lib/64bit"
        }
      }
    }
  }
}
```

See `mcp/mcp.example.json`. Copy it. Replace each path.

## Connect Claude Code

Add to `.mcp.json` in your project:

```json
{
  "mcpServers": {
    "vb": {
      "command": "python3",
      "args": ["/path/to/mcp_server.py"],
      "env": {
        "VB_REMOTE_HOST": "localhost",
        "VB_WORK_DIR": "/path/to/agent/work",
        "VB_PDK_ROOTS": "/path/to/pdks"
      }
    }
  }
}
```

## Connect any other client

Any client that speaks MCP over stdio works. Command is `python3 mcp_server.py`.
Pass the same env vars. Protocol version is `2026-07-28`.

## Settings

| Var | Job | Default |
|---|---|---|
| `VB_REMOTE_HOST` | Bridge host (`localhost` for one machine) | — |
| `VB_WORK_DIR` | Agent work area for netlists and outputs | `~/.local/state/virtuoso-bridge/work` |
| `VB_PDK_ROOTS` | PDK roots, `:` separated | `~/pdks` |
| `VB_CDS_LIB` | Extra `cds.lib` files, `:` separated | cwd + home |
| `VB_SAFE_ROOTS` | Extra allowed paths, `:` separated | — |
| `VB_INDEX_DIR` | Search index location | `~/.cache/virtuoso-bridge/index` |
| `VB_DOC_ROOTS` | Extra doc roots, `:` separated | Cadence install docs |
| `VB_DISPLAY` | X display for Virtuoso | `:0` |
| `VB_VIRTUOSO_TIMEOUT` | Bring-up wait, seconds | `600` |
| `VB_MCP_UNLIMITED` | `1` drops all rate limits | off (reads free, sims capped) |

For the bridge side (SSH targets, daemon, profiles), read
`skills/virtuoso/references/connection.md` in this repo.

## Test in 60 seconds

1. Call `startup`. Virtuoso launches if down. Wait for success.
2. Call `status`. It shows tunnel, daemon, and license state.
3. Call `search` with `{"query": "nmos_rvt", "kinds": ["cell"]}`. Hits prove the index works.
4. Call `evaluate` with `{"expr": "1+1"}`. Output `2` proves live Cadence.

All four pass: the agent can work.

## If it fails

- `startup` reports no display: start your X server, or set `VB_DISPLAY`.
- `startup` reports a dead daemon: save work, kill Virtuoso, clear the lock file, call `startup` again.
- `simulate` reports file not found: the server loads `~/.virtuoso-bridge/.env` itself; check `VB_SPECTRE_BIN` there.
- Tools missing after update: restart the session server. New tools load at startup only.
- One Virtuoso only. Never start a second one. Two instances fight over one daemon port.

## First tasks for a new agent

Load prompt `signoff-flow` before a signoff. For a diagram, install the
external [Anatomy skill](https://github.com/wheresryan22/anatomy) with
`npx skills add wheresryan22/anatomy -g` if it is not already available.
The bridge does not bundle Anatomy. Load skill `search` before discovery.
Read `skills/virtuoso/SKILL.md` before touching schematics.
