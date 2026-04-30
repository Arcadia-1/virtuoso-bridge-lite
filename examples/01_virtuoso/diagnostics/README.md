# diagnostics — filesystem-level probes for stuck Virtuoso state

Recipes that bypass SKILL APIs in favour of reading raw on-disk state.
Useful when SKILL-side enumeration disagrees with what's actually on
the filesystem (stale locks, partially-closed sessions, crashed
processes that left files behind).

## `sniff_cdslck.py`

Walks an OA library tree on the remote, finds every Cadence
``.cdslck`` lock file, prints owner + age.  More authoritative than
``maeGetSessions`` for "why is this view stuck" questions.

```bash
python sniff_cdslck.py PLAYGROUND_LLM
python sniff_cdslck.py PLAYGROUND_LLM --view maestro
```

The script never deletes locks — that's a deliberate human decision.
If you confirm a lock is stale (``ps -p <pid>`` on the lock's host
shows no process), you can ``rm`` the file manually.  Deleting a live
lock corrupts the cellview.
