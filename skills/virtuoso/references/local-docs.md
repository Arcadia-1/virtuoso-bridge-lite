# Local Documentation Protocol

Verify SKILL code and library-cell usage against the **Cadence documentation
installed on the target Virtuoso host** — before writing the code, not after it
fails in CIW.

## Why this is mandatory

- SKILL function names, signatures, and PDK device parameters are **not
  reliably included in LLM training data**. Memory-based guesses produce
  plausible-looking calls that don't exist or bind arguments wrongly.
- **Version drift is real.** The function set and documentation layout differ
  between Virtuoso releases. Observed on two live hosts:

  | | server1 | server2 |
  |---|---|---|
  | `skdfref/` style | **chapter** | **per-function** |
  | CDS python (used by index scripts) | `tools.lnx86/python/64bit/bin/python3` | same |

  A function that exists in one may be absent, renamed, or have a different
  signature in the other. Only the installed docs on *your* host are ground
  truth.
- Repo `references/` and `examples/` cover the common bridge workflows. Anything
  not there must be checked against the installed docs — **a miss in repo
  references is not evidence that a function does not exist.**

## The four verification commands

All run from the local machine; in remote mode they operate on the host from
the active profile. All support `--json`, `-p PROFILE`, `--env FILE`.

### 1. `doc-info` — identify version + doc layout (once per session/host)

```bash
virtuoso-bridge doc-info
virtuoso-bridge doc-info --json
virtuoso-bridge doc-info --doc-root /path/to/local/doc   # no bridge needed
```

Per resolved doc root it reports:

- `doc_root` / `install_root`
- `virtuoso_version` + `version_source` (`sdp` = parsed from an installed
  `*.sdp` marker file, `install_dir` = parsed from the `IC###` directory name,
  `none` = no version evidence found)
- `doc_set_count` + a sample of doc set names
- `skill_finder` — `finder/SKILL` path, presence, `.fnd` file count
- `api_more_info` — `api_more_info.tgf` path, presence, size
- `skdfref` — presence, HTML page count, **style** (`chapter` vs
  `per-function`), sample pages

Use the output to interpret everything else: the active version bounds which
functions can exist, and the `skdfref` style tells you how function pages are
organized (see "Doc-root anatomy" below).

### 2. `skill-find` — find a SKILL function, get its exact syntax

```bash
virtuoso-bridge skill-find dbOpenCellViewByType
virtuoso-bridge skill-find dbOpen --mode prefix
virtuoso-bridge skill-find "^db.*" --mode regex --json
```

Searches the installed SKILL Finder database (`doc/finder/SKILL/*.fnd`) —
downloaded to a local cache on first use. Returns name + **exact syntax** +
one-line description. If the exact name misses, try `fuzzy`/`prefix` modes and
related stems before concluding the function doesn't exist.

Python: `client.find_skill(query, mode="fuzzy"|"exact"|"prefix"|"suffix"|"regex",
limit=50, include_desc=False)`.

### 3. `skill-info` — read the full documentation page

```bash
virtuoso-bridge skill-info dbOpenCellViewByType
virtuoso-bridge skill-info maeRunSimulation --json
```

Resolves the function in `api_more_info.tgf` and renders the referenced page as
plain text: argument descriptions, return values, examples. Handles both
`skdfref` styles transparently (a chapter page is reduced to the function's
topic block; a per-function page is used as-is).

Python: `client.get_skill_more_info(func_name)` → dict with `func_name`,
`file_path`, `topic`, `raw_html`, `plain_text` (or `None` if unindexed).

### 4. `doc-search` — search every installed doc set

```bash
virtuoso-bridge doc-search "net expression label"
virtuoso-bridge doc-search "maeGetOutputValue" -n 20 --json
virtuoso-bridge doc-search --list-roots          # which roots were discovered
virtuoso-bridge doc-search --rebuild-index ...   # force index refresh
virtuoso-bridge doc-search --cache-dir /tmp/vb-docs ...
```

Builds a local SQLite index from remote doc metadata (paths/titles/topics),
then ranks matches by content. Results carry the **remote** path, title, and a
snippet — enough to decide which page to read. `--doc-root` switches to fully
local mode (no bridge, for a local Cadence install or a synced copy).

Python: `client.search_docs(query, limit=10, doc_roots=None, cache_dir=None,
rebuild_index=False)`.

## Standard verification flow

```bash
# 0. Once per session/host — pin version + layout
virtuoso-bridge doc-info

# 1. Exact name → syntax
virtuoso-bridge skill-find dbOpenCellViewByType

# 2. Full argument/return/examples docs
virtuoso-bridge skill-info dbOpenCellViewByType

# 3. Concept-level ("how do I ...?")
virtuoso-bridge doc-search "create inherited net expression label"
```

Rules of thumb:

- **Before any SKILL call you haven't used this session:** `skill-find` (cheap,
  gives syntax) → `skill-info` when argument semantics matter.
- **Before instantiating a library cell:** verify the lib/cell/view triple
  exists (`ddGetObj`), read pins/params from the live cellview, and
  `doc-search` the PDK docs for device semantics (terminal order, multi-finger
  behavior, read-only params).
- **When a call fails in CIW with "undefined function":** re-check spelling
  with `skill-find` and check `doc-info` — you may be on a version where it
  doesn't exist.
- **Never skip step 0 on a new host/profile** — the version determines what
  the other three commands can even find.

## Doc-root anatomy

A Cadence install root `<install>` (e.g. `/opt/cadence/IC231`) contains:

```
<install>/
├── doc/                              ← the "doc root" (CADENCE_DOC_ROOT)
│   ├── finder/SKILL/*.fnd            ← SKILL Finder DB (one .fnd per API area)
│   ├── api_more_info/
│   │   ├── api_more_info.tgf         ← function → (file, anchor) index
│   │   └── *.html                    ← per-area More Info pages (IC618)
│   ├── skdfref/                      ← SKILL DFII reference
│   │   ├── cvio.html, chap2.html …   ← chapter style (IC618, ~24 files)
│   │   └── *_re_<function>.html …    ← per-function style (IC231, ~1900 files)
│   ├── sklayoutref/  skcompref/  …   ← one directory per doc set
│   └── *.xml / faq / …
├── tools.lnx86/python/64bit/bin/python3   ← CDS python (index scripts)
└── Base_IC06.18.000_lnx86.sdp           ← version marker (name encodes version)
```

### Version identification

1. **SDP marker (preferred):** install roots carry `*.sdp` files whose names
   encode the version — `Base_IC06.18.000_lnx86.sdp` → **6.1.8**,
   `Hotfix_IC23.10.030_lnx86.sdp` → **23.1** (encoding: `IC` + major +
   minor×10+patch + build).
2. **Directory-name fallback:** `IC###` — for `IC618` (leading digit 5/6)
   → `6.1.8`; for `IC231` → `23.1` (two-digit major).
3. `doc-info` reports which source was used (`version_source`).

### tgf / anchor mechanics (why two styles exist)

`api_more_info.tgf` lines map a function to a page:
`<function> <file> <anchor-or-NULL> <format>`.

- **Chapter style (IC618):** `dbOpenCellViewByType skdfref/cvio.html "pgfId-5447242" HTML`
  — one large chapter file; the anchor selects the topic block. `skill-info`
  downloads the chapter page once and extracts the topic.
- **Per-function style (IC231):** `dbOpenCellViewByType cvio_re_dbOpenCellViewByType.html NULL HTML`
  — anchor is `NULL`, each function is its own page. `skill-info` uses the page
  as-is.

`doc-info`'s `skdfref.style` field is the fast way to know which style a host
uses when reading doc paths by hand.

### How doc roots are discovered (remote mode)

`doc-info` / `doc-search` / `skill-find` discover roots on the remote host by:

1. **SKILL Finder anchor:** `which virtuoso` → walk up parent directories until
   `doc/finder/SKILL` is found → its grandparent is the doc root. (Most reliable.)
2. **Environment fallback:** `CADENCE_DOC_ROOT`, `CADENCE_DOC_ROOTS`,
   `CDS_INST_DIR`, `CDSHOME`, `CDS_HOME` from the remote login environment
   (login shell + optional `VB_CADENCE_CSHRC` sourcing).

Both sources are merged and de-duplicated. Override explicitly with
`--doc-root` (local mode) or by pointing a profile at the right host.

## Caching

| What | Where | Control |
|------|-------|---------|
| `.fnd` database (skill-find) | user cache dir under `skill_finder/<host>/` | fresh host path in profile → auto re-download |
| `api_more_info.tgf` + referenced HTMLs (skill-info) | user cache dir under `skill_finder/<host>/more_info/` | re-downloaded when the remote tgf path changes |
| Doc index + downloaded match files (doc-search) | user cache dir under `docs_search/<host>/` | `--rebuild-index` (force), `--cache-dir` (move) |

`<host>` is the **GUI/documentation host** — the machine whose Cadence
installation is being read. In one-host setups that is the same host the
tunnel targets; with split `VB_GUI_HOST` / `VB_DAEMON_HOST` roles the caches
key by the GUI host, so pointing `VB_GUI_HOST` at a different install
re-downloads instead of serving the old host's cache. Discovery, indexing,
and More Info downloads all run against the GUI host — the daemon host never
needs the doc tree.

The user cache dir is overridable with the `VB_CACHE_DIR` environment variable
(useful on machines where the default AppData/`~/.cache` location is
restricted). A stale index (docs updated on the host) is the most common cause
of "doc-search misses a page I know exists" — run with `--rebuild-index`.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `doc-info` reports `virtuoso: unknown (no version evidence)` | No `*.sdp` in the install root and dir name isn't `IC###`. Harmless; docs still work. Check the install root is really a Cadence root. |
| `no Cadence doc roots found` | Profile points at the wrong host (jump host instead of compute host), or the remote login shell can't find `virtuoso` and the doc env vars are unset. Set `VB_CADENCE_CSHRC[_PROFILE]` to the cshrc that loads the Cadence env, or pass `--doc-root` explicitly. |
| `skill-find` finds nothing for a function that exists | The installed `.fnd` set doesn't cover it — try `doc-search` with the function name. |
| `skill-info` → "No More Info found" | Function isn't in `api_more_info.tgf` (common for very old or OCEAN-internal functions). Try `skill-find` (syntax still available) or `doc-search`. |
| `doc-search` misses a known page | Stale index → `--rebuild-index`. Or the root isn't discovered → check `doc-search --list-roots` against `doc-info`. |
| Remote doc scripts fail with "usable python not found" | The host has no `tools.lnx86/python/64bit/bin/python3` and no `python3`/`python` with `json` on the login-shell PATH. Install/use a CDS python or add one to PATH via `VB_CADENCE_CSHRC`. |
| `doc-search` writes fail (permission) | Cache dir not writable (restricted home/AppData) → `--cache-dir` or `VB_CACHE_DIR`. |

## Verify-before-use checklist

Before writing the SKILL:

- [ ] `doc-info` run for this host/profile (version + layout known)
- [ ] Function name confirmed via `skill-find` (exact spelling + syntax)
- [ ] Argument semantics confirmed via `skill-info` (when they matter)
- [ ] Library cell: lib/cell/view confirmed with `ddGetObj`; pins/params read
      from the live cellview, not memory
- [ ] PDK device parameters checked against PDK docs (`doc-search`) when setting
      CDF params
- [ ] Version-specific behavior noted (function may differ across releases)
