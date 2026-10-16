---
name: search
description: "Find anything in the Cadence environment with one index-backed tool call. Use for PDK cells, model files, SKILL APIs, and documentation. Prefer this over shell find, grep, or ls."
---

# Search

Use the `search` tool to discover things (`find` and `cadence_search` are its aliases). Use shell
only on paths you already hold in hand.

- Unknown location, unknown name, unknown content: call `search`.
  Examples: which lib holds a cell, where a model file lives, what a
  SKILL function is called, which doc page covers a topic.
- Known exact path: shell `grep`/`cat` on that file is fine.
  Example: grepping `.model` lines in a model file `search` already gave you.
- Never walk trees with `ls -R` or shell `find <dir>` to explore.
  That is what the index is for. One `search` call replaces it.
- Live session questions have named tools too: `list_libraries` for
  session libraries, `list_cells` for a library's cells. Never hand-roll
  ddGetLibList/ddGetObj SKILL for these.

The index is much faster than disk walks. It covers the kinds below.

## Kinds in the index

- `skill` — Cadence SKILL API entries (name, syntax, description).
- `doc` — Cadence documentation pages (title and body text).
- `topic` — Cadence topic-map entries (`.tgf`).
- `lib` — PDK and user libraries.
- `cell` — PDK and user cells, with their views.
- `file` — PDK and project files, with a text preview.

## How to use it

1. Call `search` with a plain query. Example: `search` `{"query": "nmos_rvt model"}`.
2. Add `kinds` to narrow the search. Example: `{"query": "DFFHQNx1", "kinds": ["cell"]}`.
3. Add `lib` or `cell` to pin a library or a cell.
4. Read the returned `kind`, `path`, `lib`, `cell`, and `snippet`.

## First use

The first `search` builds the index. It takes about 40 seconds.
Every later call is fast (milliseconds).
The index lives in `~/.cache/virtuoso-bridge/index/`.

## Maintenance

- `search_status` shows built kinds, row counts, size, and roots.
- `update_search` rebuilds everything. Use `update_search` after a PDK change.
- `update_search` with `kinds` rebuilds only those kinds.
- `update_search` with `rebuild` drops the index first.

## Aliases

- `search_docs` is `search` with `kinds` set to `doc` and `topic`.
- `lookup_skill` is `search` with `kinds` set to `skill`.
- `search` and `find` are aliases of `search`.

## Result rules

- Always quote the exact `path` from the hit when you use a file.
- Do not guess a path. Confirm it with `search` first.
- If a query returns no hits, rebuild with `update_search`.
