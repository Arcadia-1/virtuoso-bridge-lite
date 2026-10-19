---
name: search
description: "Build and query an optional local SQLite index of Cadence docs, SKILL entries, libraries, cells and project files."
---

# Optional unified search

Use the installed Python package in the project virtual environment:

```sh
python -m virtuoso_bridge.unified_search status
python -m virtuoso_bridge.unified_search build --kinds skill doc
python -m virtuoso_bridge.unified_search search dbOpenCellViewByType
```

Build only the kinds needed: `skill`, `doc`, `topic`, `lib`, `cell`, `file`.
Index construction reads the local configured roots and writes a local cache;
it does not connect to Virtuoso or require MCP. Build time depends on root size.
Use `build --rebuild` to discard and rebuild the cache after source changes.
Refresh and rebuild publish one SQLite transaction. If collection is interrupted,
queries continue to see the previous complete index. The manifest stored in the
database commits with the indexed rows; `manifest.json` is a readable mirror.
Existing bridge search APIs remain available without this optional index.

Python callers may pass explicit `roots` to `build` and filter `search` by
`kinds`, `lib` or `cell`. Use returned paths when inspecting results. Empty
results are not evidence that an API or cell is absent from the installation.
