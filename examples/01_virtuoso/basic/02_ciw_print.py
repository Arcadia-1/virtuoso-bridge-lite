#!/usr/bin/env python3
"""Print Shakespeare's Sonnet 18 to the Virtuoso CIW window.

Demonstrates printing multi-line text to CIW via execute_skill().
Each line is sent as a separate printf() call — batching multiple
printf() in a single execute_skill() loses newlines.

Compare with 03_load_il.py which prints the same sonnet by loading
a .il file directly.

Prerequisites:
- virtuoso-bridge tunnel running (virtuoso-bridge start)
- RAMIC daemon loaded in Virtuoso CIW
"""
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()

sonnet = """\

========================================================
  Sonnet 18  by William Shakespeare
========================================================

  Shall I compare thee to a summer's day?
  Thou art more lovely and more temperate:
  Rough winds do shake the darling buds of May,
  And summer's lease hath all too short a date:
  Sometime too hot the eye of heaven shines,
  And often is his gold complexion dimm'd;
  And every fair from fair sometime declines,
  By chance, or nature's changing course untrimm'd;
  But thy eternal summer shall not fade,
  Nor lose possession of that fair thou ow'st;
  Nor shall Death brag thou wander'st in his shade,
  When in eternal lines to Time thou grow'st:
    So long as men can breathe, or eyes can see,
    So long lives this, and this gives life to thee.

========================================================"""

for line in sonnet.splitlines():
    r = client.execute_skill('printf("' + line + '\\n")')
    if r.status.value != "success":
        print(f"Error: {r.errors}")

print("Done. Check Virtuoso CIW for Sonnet 18.")
