#!/bin/bash
# Use the same project interpreter as the MCP client. No Cadence required.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON:-python3}" "$ROOT/mcp/smoke.py"
