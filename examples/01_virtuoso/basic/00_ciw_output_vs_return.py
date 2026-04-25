#!/usr/bin/env python3
"""CIW output vs Python return value — understand the difference.

execute_skill() evaluates a SKILL expression on the remote Virtuoso and
returns the result to Python.  It does NOT automatically print anything
in the CIW window.  To display text in CIW, use printf() explicitly.

Prerequisites:
- virtuoso-bridge tunnel running (virtuoso-bridge start)
- RAMIC daemon loaded in Virtuoso CIW
"""
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()

# ── 1. Return value only (nothing shows in CIW) ─────────────────────
# The result 3 is sent back to Python; CIW stays silent.
r = client.execute_skill("1+2")
print(f"[Python] 1+2 = {r.output}")
#   Python console:  [Python] 1+2 = 3
#   CIW window:      (nothing)

# ── 2. CIW output + return value ────────────────────────────────────
# printf() prints to CIW and returns t; the let() block returns the
# computed value to Python so you get both.
r = client.execute_skill(r'let((v) v=1+2 printf("1+2 = %d\n" v) v)')
print(f"[Python] 1+2 = {r.output}")
#   Python console:  [Python] 1+2 = 3
#   CIW window:      1+2 = 3
