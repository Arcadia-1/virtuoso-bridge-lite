#!/bin/bash
# Smoke test: talks to mcp_server.py over stdio, checks 2026-07-28 surface.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}' \
 '{"jsonrpc":"2.0","id":3,"method":"skills/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}' \
 '{"jsonrpc":"2.0","id":4,"method":"prompts/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}' \
 | timeout 20 python3.12 "$ROOT/mcp_server.py" | python3.12 -c 'import sys,json; rs={}; [rs.update({json.loads(l)["id"]:json.loads(l)}) for l in sys.stdin if l.strip().startswith("{")]; assert rs[1]["result"]["protocolVersions"][0]=="2026-07-28"; assert len(rs[2]["result"]["tools"])==31; assert len(rs[3]["result"]["skills"])==5; assert len(rs[4]["result"]["prompts"])==6; print("SMOKE_OK tools=31 skills=5 prompts=6")'
