"""Unified index-backed search across the whole Cadence environment.

An agent should not shell out with ``find``/``grep`` across gigabytes of
PDK and documentation.  It should make one fast tool call.  This module
builds a single SQLite FTS5 database that holds every resource an
engineering agent needs, and answers queries in milliseconds.

Indexed kinds
-------------
``skill``  Cadence SKILL API entries   (``doc/finder/SKILL/*.fnd``)
``doc``    Cadence documentation pages (doc roots: html/txt/xml/json)
``topic``  Cadence topic-map entries   (``*.tgf``)
``lib``    PDK / user libraries        (cds.lib + install + root scan)
``cell``   PDK / user library cells    (OpenAccess dirs, one row per cell)
``file``   PDK and project files       (path + text preview for text files)

Design
------
* stdlib only (``sqlite3`` with the FTS5 extension).
* Build once, cache on disk, then search in milliseconds.
* Kinds are filterable, so an agent can ask for only cells or only docs.
* Rebuild is append-per-kind: the fast kinds build in seconds, ``doc``
  (gigabytes) builds on demand and every other kind stays usable.

Command line (also used by the MCP server when it shells out)::

    python -m virtuoso_bridge.unified_search status
    python -m virtuoso_bridge.unified_search build --kinds skill lib cell
    python -m virtuoso_bridge.unified_search search "nmos nfin"
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Sequence

SCHEMA_VERSION = 2
FORMAT = "vb-unified-search"

KINDS = ("skill", "doc", "topic", "lib", "cell", "file")

# Serializes index builds inside one process. Two parallel tool calls must
# not DELETE+INSERT on the same SQLite file at once ("database is locked").
_BUILD_LOCK = threading.Lock()

KIND_HELP = {
    "skill": "Cadence SKILL API function entry (name, syntax, description)",
    "doc": "Cadence documentation page (html/txt/xml/json title + body)",
    "topic": "Cadence topic-map entry (.tgf)",
    "lib": "PDK or user library (name, path, cell count)",
    "cell": "PDK or user library cell (lib, cell, views)",
    "file": "PDK or project file (path + text preview)",
}

TEXT_SUFFIXES = {
    ".pm", ".lib", ".scs", ".sp", ".spi", ".cdl", ".cir", ".ckt", ".net",
    ".il", ".ils", ".tcl", ".ocn", ".sci", ".py", ".sh", ".json", ".md",
    ".txt", ".csv", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".tech",
    ".tf", ".map", ".rf", ".def", ".lef", ".gds.txt", ".param", ".va",
    ".vams", ".scs.template", ".sv", ".v",
}
DOC_SUFFIXES = {".html", ".htm", ".txt", ".xml", ".json"}
FILE_PREVIEW_LIMIT = 16384
QUERY_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
    "for", "from", "how", "in", "into", "is", "it", "of", "on", "or", "the",
    "to", "what", "when", "where", "which", "who", "why", "with",
}


# ---------------------------------------------------------------- locations

def cache_root() -> Path:
    """Directory that holds the index database and its manifest."""
    env = os.environ.get("VB_INDEX_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "virtuoso-bridge" / "index"


def db_path() -> Path:
    return cache_root() / "search.sqlite"


def manifest_path() -> Path:
    return cache_root() / "manifest.json"


def _cds_install() -> Path:
    for name in ("CDS_INST_DIR", "CDSHOME", "CDS_HOME"):
        val = os.environ.get(name)
        if val:
            return Path(val)
    return Path("/opt/cadence")


def _split_env_paths(value: str | None) -> list[Path]:
    if not value:
        return []
    return [Path(p).expanduser() for p in value.split(os.pathsep) if p]


def default_roots() -> dict[str, list[Path]]:
    """Resolve every source root from the environment.

    Doc and skill roots come from the Cadence install (``$CDS_INST_DIR``,
    which also covers ``<install>/<tool>/doc`` trees).  PDK and project
    roots come from ``VB_PDK_ROOTS`` / ``VB_FILE_ROOTS``.
    """
    cds = _cds_install()

    doc_roots: list[Path] = []
    for p in _split_env_paths(os.environ.get("VB_DOC_ROOTS")):
        if p.is_dir():
            doc_roots.append(p)
    for cand in [cds / "doc", *sorted(cds.glob("*/doc"))]:
        if cand.is_dir() and cand not in doc_roots:
            doc_roots.append(cand)

    skill_roots: list[Path] = []
    for p in _split_env_paths(os.environ.get("VB_SKILL_ROOTS")):
        if p.is_dir():
            skill_roots.append(p)
    for cand in [cds / "doc" / "finder" / "SKILL",
                 *sorted(cds.glob("*/doc/finder/SKILL"))]:
        if cand.is_dir() and cand not in skill_roots:
            skill_roots.append(cand)

    pdk_roots: list[Path] = []
    for p in _split_env_paths(os.environ.get("VB_PDK_ROOTS")):
        if p.is_dir():
            pdk_roots.append(p)
    if not pdk_roots:
        cand = Path.home() / "pdks"
        if cand.is_dir():
            pdk_roots.append(cand)

    file_roots: list[Path] = []
    for p in _split_env_paths(os.environ.get("VB_FILE_ROOTS")):
        if p.is_dir():
            file_roots.append(p)

    return {"doc": doc_roots, "skill": skill_roots,
            "pdk": pdk_roots, "file": file_roots}


# ---------------------------------------------------------------- helpers

def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_head(path: Path, limit: int) -> str:
    """Decode only the first *limit* bytes; never read a huge file whole."""
    with path.open("rb") as fh:
        data = fh.read(limit)
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _html_text(raw: str) -> tuple[str, str]:
    """Return (title, text) from an HTML document, scripts/styles stripped."""
    from html import unescape
    from html.parser import HTMLParser

    class _X(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self._t = 0
            self._title: list[str] = []
            self._body: list[str] = []
            self._skip = 0

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag == "title":
                self._t += 1
            elif tag in ("script", "style"):
                self._skip += 1

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag == "title" and self._t:
                self._t -= 1
            elif tag in ("script", "style") and self._skip:
                self._skip -= 1

        def handle_data(self, data):
            if self._skip:
                return
            (self._title if self._t else self._body).append(data)

    if "<title" not in raw.lower() and "<html" not in raw.lower():
        return Path("x").stem, unescape(_squash(raw))
    p = _X()
    try:
        p.feed(raw)
    except Exception:
        return "", unescape(_squash(raw))
    title = _squash(" ".join(p._title))
    body = _squash(" ".join(p._body))
    return title, body or unescape(_squash(raw))


def _fts_query(query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression."""
    tokens = re.findall(r"[A-Za-z0-9_.$:+-]+", query or "")
    keep = [t for t in tokens if t.lower() not in QUERY_STOPWORDS]
    tokens = keep or tokens
    parts = []
    for tok in tokens:
        tok = tok.strip(".")
        if not tok:
            continue
        tok = tok.replace('"', '""')
        parts.append('"%s"*' % tok)
    return " AND ".join(parts)


# ---------------------------------------------------------------- collectors

def _iter_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for name in filenames:
            yield Path(dirpath) / name


def collect_skill(roots: dict, body_limit: int) -> Iterator[dict]:
    try:
        from virtuoso_bridge.virtuoso.skill_finder.parser import parse_fnd_directory
    except Exception:
        return
    seen: set[str] = set()
    for root in roots.get("skill", []):
        try:
            entries = parse_fnd_directory(root)
        except Exception:
            continue
        for e in entries:
            if not e.name or e.name in seen:
                continue
            seen.add(e.name)
            yield {"kind": "skill", "title": e.name, "path": e.source_file or str(root),
                   "body": ("%s %s" % (e.syntax, e.description))[:body_limit]}


def collect_doc(roots: dict, body_limit: int) -> Iterator[dict]:
    for root in roots.get("doc", []):
        if not root.is_dir():
            continue
        for path in _iter_files(root):
            suffix = path.suffix.lower()
            if suffix == ".tgf":
                yield from _tgf_rows(path, root)
                continue
            if suffix not in DOC_SUFFIXES:
                continue
            try:
                raw = _read_text(path)
            except OSError:
                continue
            if suffix in (".html", ".htm"):
                title, text = _html_text(raw)
            else:
                title, text = path.stem, _squash(raw)
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.as_posix()
            yield {"kind": "doc", "title": title or path.stem, "path": str(path),
                   "body": ("%s %s" % (rel, text))[:body_limit]}


def _tgf_rows(path: Path, root: Path) -> Iterator[dict]:
    try:
        raw = _read_text(path)
    except OSError:
        return
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[-1].upper() != "HTML":
            continue
        topic_id, target, anchor = parts[0], parts[1], parts[2]
        yield {"kind": "topic", "title": anchor or Path(target).stem,
               "path": str(path),
               "body": "%s %s %s" % (topic_id, anchor, target)}


def _oa_cells(lib: Path, cap: int = 50000) -> dict[str, list[str]]:
    cells: dict[str, list[str]] = {}
    try:
        entries = list(lib.iterdir())
    except OSError:
        return cells
    for cell in entries:
        if len(cells) >= cap:
            break
        if not cell.is_dir() or cell.is_symlink():
            continue
        try:
            boys = list(cell.iterdir())
        except OSError:
            continue
        names = {b.name for b in boys}
        if "data.dm" not in names:
            continue
        cells[cell.name] = sorted(b.name for b in boys if b.is_dir())
    return cells


def _cds_lib_files() -> list[Path]:
    """cds.lib files to read, in order: cwd, VB_CDS_LIB extras, home."""
    out = [Path.cwd() / "cds.lib"]
    for p in _split_env_paths(os.environ.get("VB_CDS_LIB")):
        out.append(p)
    out.append(Path.home() / "cds.lib")
    return out


def _discover_libs(roots: dict) -> list[tuple[str, Path]]:
    """Find (lib, path) pairs under the PDK roots (and cds.lib DEFINEs)."""
    defs: dict[str, Path] = {}
    for cds in _cds_lib_files():
        if not cds.is_file():
            continue
        try:
            text = cds.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split()
            if parts and parts[0] == "DEFINE" and len(parts) >= 3:
                p = Path(os.path.expandvars(parts[2].replace("$CDS", "/opt/cadence")))
                if not p.is_absolute():
                    p = cds.parent / p
                if p.is_dir():
                    defs[parts[1]] = p

    cands: dict[str, Path] = dict(defs)
    for root in roots.get("pdk", []):
        if not root.is_dir():
            continue
        for sub in ("", "libs.ref", "libs"):
            base = root / sub if sub else root
            if not base.is_dir():
                continue
            try:
                kids = list(base.iterdir())
            except OSError:
                continue
            for k in kids:
                if k.is_dir() and not k.is_symlink() and k.name not in cands:
                    if (k / "cdsinfo.tag").exists() or (k / "tech.db").exists():
                        cands[k.name] = k
    return sorted(cands.items())


def collect_lib_cell(roots: dict, body_limit: int) -> Iterator[dict]:
    for name, path in _discover_libs(roots):
        cells = _oa_cells(path)
        yield {"kind": "lib", "title": name, "lib": name, "path": str(path),
               "body": "%s %s %d cells" % (name, path, len(cells))}
        for cell, views in cells.items():
            yield {"kind": "cell", "title": cell, "lib": name, "cell": cell,
                   "view": "|".join(views), "path": str(path),
                   "body": "%s %s %s" % (cell, name, " ".join(views))}


def collect_file(roots: dict, body_limit: int) -> Iterator[dict]:
    seen: set[str] = set()
    for root in [*roots.get("pdk", []), *roots.get("file", [])]:
        if not root.is_dir():
            continue
        for path in _iter_files(root):
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            suffix = path.suffix.lower()
            body = str(path)
            if suffix in TEXT_SUFFIXES:
                try:
                    body = "%s %s" % (path.name, _read_head(path, FILE_PREVIEW_LIMIT))
                except OSError:
                    pass
            yield {"kind": "file", "title": path.name, "path": str(path),
                   "body": body[:body_limit]}


# One collector can emit several kinds (a doc walk also finds .tgf topics;
# a lib scan also emits the cells).  Group the kinds so each collector runs
# exactly once and every kind it emits is cleared before it runs.
GROUPS: list[tuple[str, tuple[str, ...], Callable]] = [
    ("skill", ("skill",), collect_skill),
    ("doc", ("doc", "topic"), collect_doc),
    ("libcell", ("lib", "cell"), collect_lib_cell),
    ("file", ("file",), collect_file),
]


def _groups_for(kinds: Sequence[str]) -> list[tuple[str, tuple[str, ...], Callable]]:
    want = set(kinds)
    return [g for g in GROUPS if want.intersection(g[1])]


# ---------------------------------------------------------------- schema

def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS entries USING fts5(
            kind UNINDEXED,
            path UNINDEXED,
            lib UNINDEXED,
            cell UNINDEXED,
            view UNINDEXED,
            title,
            body,
            tokenize = 'unicode61'
        );
        """
    )


def _row_values(row: dict, body_limit: int) -> tuple:
    return (
        row.get("kind", ""), row.get("path", ""), row.get("lib", ""),
        row.get("cell", ""), row.get("view", ""),
        _squash(row.get("title", ""))[:400],
        (row.get("body", "") or "")[:body_limit],
    )


# ---------------------------------------------------------------- build

def build(
    *,
    kinds: Sequence[str] | None = None,
    roots: dict | None = None,
    rebuild: bool = False,
    body_limit: int = 16000,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Build or refresh the index for *kinds*.  Returns a manifest dict."""
    kinds = list(kinds or KINDS)
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ValueError("unknown index kind(s): %s (valid: %s)"
                         % (", ".join(unknown), ", ".join(KINDS)))
    roots = roots or default_roots()
    db = db_path()
    man = manifest_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    if rebuild and db.exists():
        db.unlink()

    counts: dict[str, int] = {}
    rebuilt: set[str] = set()
    with _BUILD_LOCK:
        con = _connect(db)
        try:
            _create_schema(con)
            for name, kinds_in_group, collector in _groups_for(kinds):
                for kind in kinds_in_group:
                    con.execute("DELETE FROM entries WHERE kind = ?", (kind,))
                if progress:
                    progress("indexing %s ..." % "+".join(kinds_in_group))
                started = time.monotonic()
                batch: list[tuple] = []
                for row in collector(roots, body_limit):
                    batch.append(_row_values(row, body_limit))
                    if len(batch) >= 2000:
                        con.executemany(
                            "INSERT INTO entries (kind,path,lib,cell,view,title,body) "
                            "VALUES (?,?,?,?,?,?,?)", batch)
                        batch.clear()
                if batch:
                    con.executemany(
                        "INSERT INTO entries (kind,path,lib,cell,view,title,body) "
                        "VALUES (?,?,?,?,?,?,?)", batch)
                con.commit()
                rebuilt.update(kinds_in_group)
                if progress:
                    got = _count_group(con, kinds_in_group)
                    progress("  %s: %d rows in %.1fs"
                             % ("+".join(kinds_in_group), got, time.monotonic() - started))
            counts = _count_by_kind(con)
        finally:
            con.close()

    prev = _read_manifest() or {}
    prev_kinds = prev.get("kinds", {}) if not rebuild else {}
    # Kinds rebuilt now take fresh DB counts (zero drops them, so
    # "missing" stays accurate). Kinds not touched keep old numbers.
    merged = {k: v for k, v in prev_kinds.items() if k not in rebuilt and v}
    for k in rebuilt:
        if counts.get(k):
            merged[k] = counts[k]
    manifest = {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "body_limit": body_limit,
        "kinds": merged,
        "roots": {k: [str(p) for p in v] for k, v in roots.items()},
    }
    man.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest


def _count_group(con: sqlite3.Connection, kinds: Sequence[str]) -> int:
    marks = ",".join("?" * len(kinds))
    row = con.execute(
        "SELECT count(*) FROM entries WHERE kind IN (%s)" % marks, list(kinds)).fetchone()
    return int(row[0]) if row else 0


def _count_by_kind(con: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for kind, n in con.execute("SELECT kind, count(*) FROM entries GROUP BY kind"):
        out[kind] = n
    return out


def _read_manifest() -> dict | None:
    try:
        return json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _built_kinds(manifest: dict | None) -> set[str]:
    if not manifest or manifest.get("schema_version") != SCHEMA_VERSION:
        return set()
    return {k for k, n in (manifest.get("kinds") or {}).items() if n}


# ---------------------------------------------------------------- search

def available() -> bool:
    return db_path().is_file() and manifest_path().is_file()


def ensure(
    *,
    kinds: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Make sure the requested kinds exist.  Builds only what is missing."""
    man = _read_manifest()
    built = _built_kinds(man)
    need = list(kinds or KINDS)
    missing = [k for k in need if k not in built]
    if missing:
        build(kinds=missing, progress=progress)
        man = _read_manifest()
    return man or {}


def search(
    query: str,
    *,
    kinds: Sequence[str] | None = None,
    limit: int = 20,
    lib: str | None = None,
    cell: str | None = None,
) -> list[dict]:
    """Query the index.  Returns ranked hits with a highlighted snippet."""
    if not available():
        return []
    match = _fts_query(query)
    if not match:
        return []
    where = ["entries MATCH ?"]
    params: list = [match]
    if kinds:
        where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        params.extend(kinds)
    if lib:
        where.append("lib = ?")
        params.append(lib)
    if cell:
        where.append("cell = ?")
        params.append(cell)
    sql = (
        "SELECT kind, path, lib, cell, view, title, "
        "snippet(entries, 6, '[', ']', '…', 14) AS snip, "
        "bm25(entries, 0,0,0,0,0, 6.0, 1.0) AS rank "
        "FROM entries WHERE " + " AND ".join(where) +
        " ORDER BY rank LIMIT ?"
    )
    params.append(int(limit))
    con = _connect(db_path())
    try:
        rows = list(con.execute(sql, params))
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    hits = []
    for kind, path, lib_, cell_, view, title, snip, rank in rows:
        hits.append({"kind": kind, "title": title, "path": path,
                     "lib": lib_ or None, "cell": cell_ or None,
                     "view": view or None, "snippet": snip,
                     "score": round(-float(rank), 2)})
    return hits


def status() -> dict:
    man = _read_manifest() or {}
    built = _built_kinds(man)
    size = db_path().stat().st_size if db_path().is_file() else 0
    return {
        "available": available(),
        "db": str(db_path()),
        "size_bytes": size,
        "schema_version": man.get("schema_version"),
        "built_at": man.get("built_at"),
        "counts": man.get("kinds", {}),
        "built_kinds": sorted(built),
        "missing_kinds": [k for k in KINDS if k not in built],
        "roots": man.get("roots", {}),
        "kind_help": KIND_HELP,
    }


# ---------------------------------------------------------------- CLI

def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if cmd == "build":
        kinds: list[str] = []
        rebuild = False
        i = 1
        while i < len(argv):
            if argv[i] == "--kinds":
                i += 1
                while i < len(argv) and not argv[i].startswith("--"):
                    kinds.append(argv[i])
                    i += 1
                continue
            if argv[i] == "--rebuild":
                rebuild = True
            i += 1
        try:
            man = build(kinds=kinds or None, rebuild=rebuild,
                        progress=lambda m: print(m, file=sys.stderr))
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        print(json.dumps({"counts": man["kinds"], "built_at": man["built_at"]}, indent=2))
        return 0
    if cmd == "search":
        query = " ".join(argv[1:])
        for hit in search(query):
            loc = hit["path"]
            print("%-6s %-40s %s" % (hit["kind"], hit["title"], loc))
            if hit["snippet"]:
                print("        " + hit["snippet"])
        return 0
    print("usage: python -m virtuoso_bridge.unified_search "
          "{status|build [--kinds ...] [--rebuild]|search <query>}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
