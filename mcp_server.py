#!/usr/bin/env python3
"""virtuoso-bridge MCP server — 2026-07-28 core + tasks/skills extensions.

Stdlib only. No extra install. Works on Python >= 3.9.

Transports: stdio (JSON-RPC 2.0, one message per line).
Modern clients use ``server/discover``. Legacy clients use ``initialize``.
Both are supported. Every request SHOULD carry ``params._meta`` with
``io.modelcontextprotocol/protocolVersion``. Every result carries
``resultType: complete`` and ``_meta.serverInfo``.

Long jobs (simulate, simulate_many, run_test, capture_session) use the
official tasks extension when the client opts in. Otherwise they run
synchronously with a timeout.

Logs go to stderr only. Never to stdout.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import logging
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

logger = logging.getLogger("virtuoso-bridge-mcp")
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

BASE = Path(__file__).resolve().parent
SKILLS_DIR = BASE / "skills"


def _load_bridge_env() -> None:
    """Load the bridge .env (same file the CLI uses) without overriding.

    The harness launches this server with a small env. Spectre binary,
    remote host, and license vars live in ~/.virtuoso-bridge/.env. Without
    this, simulate fails with 'file not found' for every agent.
    Harness-provided vars always win. Stdlib-only parser on purpose.
    """
    import os
    cands = [BASE / ".env", Path.home() / ".virtuoso-bridge" / ".env"]
    for cand in cands:
        try:
            text = cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "VB_REMOTE_HOST" not in text and "VB_SPECTRE_BIN" not in text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if k.startswith("VB_") and k not in os.environ:
                os.environ[k] = v
        break


_load_bridge_env()

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2024-11-05"]
SERVER_INFO = {"name": "virtuoso-bridge", "version": "1.1.0"}
SERVER_TITLE = "Virtuoso Bridge — Cadence Virtuoso + Spectre"

TASKS_EXT = "io.modelcontextprotocol/tasks"
SKILLS_EXT = "io.modelcontextprotocol/skills"
META_VER = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"

PAGE_SIZE = 50
TEXT_LIMIT = 12000
STRUCT_LIMIT = 200000  # chars of JSON for structuredContent

# ---------------------------------------------------------------- timeouts
# Short SKILL calls stay fast. Spectre/transient runs need hours, not
# minutes: 600 s kills real noised long-tran jobs mid-run (see skills/spectre).
TIMEOUT_SKILL = 60
TIMEOUT_SPECTRE = 3600
TIMEOUT_SNAPSHOT = 300

# ---------------------------------------------------------------- rate limit
# Local single-operator box: reads are unlimited. Limits exist only where a
# runaway loop costs real money or state: licenses (sims), GUI grabs, and
# heavy builds. VB_MCP_UNLIMITED=1 disables even those.
_UNLIMITED = False
try:
    import os as _os
    _UNLIMITED = (_os.environ.get("VB_MCP_UNLIMITED") or "").strip() == "1"
except Exception:
    pass

# Simple per-tool token bucket. Prevents an agent loop from hammering the
# CIW or the license server. Only expensive tools are listed; everything
# else is unlimited.
_RATE: dict[str, list[float]] = {}
_RATE_LOCK = threading.Lock()

_RATE_LIMITS = {
    "simulate": 30, "simulate_many": 30, "run_test": 30,
    "capture_session": 30, "screenshot": 60,
    "add_part": 60, "add_wire": 60,
    "add_pin": 60,
    # Builds are heavy.
    "update_search": 6,
    # Bridge bring-up is slow and stateful; do not hammer it.
    "startup": 6,
}


def _rate_ok(name: str, limit: int = 30, window: float = 60.0) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(name, []) if now - t < window]
        if len(hits) >= limit:
            _RATE[name] = hits
            return False
        hits.append(now)
        _RATE[name] = hits
        return True


# ---------------------------------------------------------------- helpers

def _meta_of(params: dict) -> dict:
    return (params or {}).get("_meta", {}) or {}


def _client_supports_tasks(params: dict) -> bool:
    caps = _meta_of(params).get(META_CAPS, {}) or {}
    ext = caps.get("extensions", {}) or {}
    return TASKS_EXT in ext


def _version_of(params: dict) -> str:
    return _meta_of(params).get(META_VER, PROTOCOL_VERSION) or PROTOCOL_VERSION


def _base_result(extra: dict, cache_public: bool = True, ttl: int = 60000) -> dict:
    extra = dict(extra)
    extra.setdefault("resultType", "complete")
    extra["_meta"] = {META_SERVER: SERVER_INFO}
    if "ttlMs" not in extra:
        extra["ttlMs"] = ttl
    if "cacheScope" not in extra:
        extra["cacheScope"] = "public" if cache_public else "private"
    return extra


def _text_result(text: str, structured=None, cache_public: bool = False) -> dict:
    text = text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + "\n…[truncated]"
    out: dict = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        try:
            s = json.dumps(structured, default=str)
            if len(s) > STRUCT_LIMIT:
                structured = {"_truncated": True, "hint": "use read_waveform with signals"}
            out["structuredContent"] = structured
        except Exception:
            pass
    return _base_result(out, cache_public=cache_public)


def _tool_error(text: str) -> dict:
    r = {"content": [{"type": "text", "text": "ERROR: " + text[:4000]}],
         "isError": True}
    return _base_result(r, cache_public=False, ttl=0)


def _rpc_error(code: int, message: str, data=None) -> dict:
    e: dict = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return e


def _page(items: list, cursor: str | None, extra_key: str) -> tuple[list, str | None]:
    start = 0
    if cursor:
        try:
            start = int(base64.b64decode(cursor.encode()).decode())
        except Exception:
            start = 0
    chunk = items[start:start + PAGE_SIZE]
    nxt = None
    if start + PAGE_SIZE < len(items):
        nxt = base64.b64encode(str(start + PAGE_SIZE).encode()).decode()
    return chunk, nxt


def _env_paths(var: str) -> list[Path]:
    """Paths from an os.pathsep-separated env var. Empty when unset."""
    import os
    out: list[Path] = []
    for v in (os.environ.get(var) or "").split(os.pathsep):
        if v.strip():
            out.append(Path(v.strip()).expanduser())
    return out


def _work_dir() -> Path:
    """Agent work area. VB_WORK_DIR wins; else an XDG-style state dir."""
    import os
    v = (os.environ.get("VB_WORK_DIR") or "").strip()
    if v:
        return Path(v).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "virtuoso-bridge" / "work"


def _pdk_roots() -> list[Path]:
    """PDK roots. VB_PDK_ROOTS wins; else ~/pdks."""
    roots = _env_paths("VB_PDK_ROOTS")
    if not roots:
        roots = [Path.home() / "pdks"]
    return roots


def _cds_lib_files() -> list[Path]:
    """cds.lib files to read, in order: cwd, VB_CDS_LIB extras, home."""
    return [Path.cwd() / "cds.lib", *_env_paths("VB_CDS_LIB"), Path.home() / "cds.lib"]


def _safe_roots() -> list[Path]:
    """Directories an agent may read/write through file-taking tools.

    Covers the agent loop on any machine without code changes: repo
    checkout (BASE), agent cwd, agent work area (VB_WORK_DIR), PDK roots
    (VB_PDK_ROOTS), and the Cadence install. Extra machine-specific roots
    come from VB_SAFE_ROOTS. Everything else is rejected.
    """
    import os
    roots: list[Path] = [BASE, Path.cwd(), _work_dir(), *_pdk_roots()]
    roots.extend(_env_paths("VB_SAFE_ROOTS"))
    for var in ("CDS_INST_DIR", "CDSHOME"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]))
    roots.append(Path("/opt/cadence"))
    out: list[Path] = []
    for r in roots:
        try:
            rp = r.resolve()
        except Exception:
            continue
        if rp not in out:
            out.append(rp)
    return out


def _safe_path(p: str, must_exist: bool = True) -> Path:
    """Resolve user path. Reject anything outside _safe_roots()."""
    q = (Path(p).expanduser() if Path(p).is_absolute() else (Path.cwd() / p)).resolve()
    if must_exist and not q.exists():
        raise ValueError("file not found: %s" % p)
    if not any(q == r or r in q.parents for r in _safe_roots()):
        raise ValueError("path outside allowed roots: %s" % p)
    return q


def _safe_out_dir(p: str | None, default: str) -> Path:
    """Resolve an output directory inside the allowed roots; create it."""
    d = _safe_path(p or default, must_exist=False)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _redact(text: str) -> str:
    for key in ("VB_DAEMON_TOKEN", "token=", "Bearer "):
        if key in text:
            text = text.replace(key, key + "***")
    return text


# ---------------------------------------------------------------- tool table
# Deterministic order (sorted by name). Improves prompt-cache hits.

def _tool_defs() -> list[dict]:
    obj = {"type": "object"}
    t = [
        ("status", "Tunnel, daemon and Spectre license state. Read-only.",
         {"type": "object", "properties": {}, "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("startup", "Full bring-up on this device: launch Virtuoso if down (needs a live X display, VB_DISPLAY, default :0), wait until it answers SKILL, then set up tunnel + daemon. Work dir defaults to the cds.lib dir (VB_CDS_LIB) or VB_WORK_DIR. Timeout via VB_VIRTUOSO_TIMEOUT (default 600 s). Bring-up flow for any agent: startup, status, then work.",
         {"type": "object", "properties": {}, "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
        ("search_docs", "Search installed Cadence documentation (index-backed). Alias of search with kinds=[doc,topic].",
         {"type": "object", "properties": {
             "query": {"type": "string", "minLength": 2, "maxLength": 300},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
          "required": ["query"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("search", "ONE index-backed search over everything in Cadence: SKILL APIs, Cadence docs, topic maps, PDK/user libraries, PDK/user cells, and PDK/project files. Use this (not generic web/file search) for ANY Cadence question. Returns ranked hits with kind, path and a highlighted snippet.",
         {"type": "object", "properties": {
             "query": {"type": "string", "minLength": 2, "maxLength": 300,
                       "description": "Free text. Identifiers, cell names, net names, function names, file contents."},
             "kinds": {"type": "array", "description": "Restrict to these resource kinds. Omit for all.",
                       "items": {"type": "string", "enum": ["skill", "doc", "topic", "lib", "cell", "file"]}},
             "lib": {"type": "string", "maxLength": 200, "description": "Only rows of this library."},
             "cell": {"type": "string", "maxLength": 200, "description": "Only rows of this cell."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
             "refresh": {"type": "boolean", "default": False,
                         "description": "Rebuild the index before searching (slow)."}},
          "required": ["query"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("list_libraries", "Libraries visible in the running Virtuoso session. Use instead of hand-rolled ddGetLibList SKILL.",
         {"type": "object", "properties": {}, "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("list_cells", "Cells (with views) of one library in the running session. Use instead of hand-rolled ddGetObj SKILL.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200}},
          "required": ["lib"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("search_status", "Show the unified search index: built kinds, row counts, size, build time, source roots.",
         {"type": "object", "properties": {}, "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("update_search", "Build or refresh the unified search index. Omit kinds to build all. Use rebuild to drop first.",
         {"type": "object", "properties": {
             "kinds": {"type": "array",
                       "items": {"type": "string", "enum": ["skill", "doc", "topic", "lib", "cell", "file"]}},
             "rebuild": {"type": "boolean", "default": False}},
          "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
        ("test_results", "Read scalar results of one named Maestro test from its history. For waveform values use read_waveform.",
         {"type": "object", "properties": {
             "test": {"type": "string", "minLength": 1, "maxLength": 200},
             "history": {"type": "string", "maxLength": 200}},
          "required": ["test"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("run_test", "Run one named Maestro test from a session history (test NAME, not a file). For .scs netlists use simulate instead.",
         {"type": "object", "properties": {
             "test": {"type": "string", "minLength": 1, "maxLength": 200},
             "timeout": {"type": "integer", "minimum": 60, "maximum": 7200, "default": 1800}},
          "required": ["test"], "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
        ("check_netlist", "Run semantic netlist checker in dut or tb mode.",
         {"type": "object", "properties": {
             "file": {"type": "string", "minLength": 1, "maxLength": 500},
             "mode": {"type": "string", "enum": ["dut", "tb"], "default": "dut"}},
          "required": ["file"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("describe_pdk", "Detail one PDK lib: path, cell and view counts, techfile, models, sample cells.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200}},
          "required": ["lib"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("list_pdk", "List PDK libs visible to Virtuoso: cells, symbols, techfile, models. Cached.",
         {"type": "object", "properties": {}, "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("search_pdk", "Find cells by name across PDK libs. Returns lib, cell, views.",
         {"type": "object", "properties": {
             "query": {"type": "string", "minLength": 2, "maxLength": 200},
             "lib": {"type": "string", "maxLength": 200},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
          "required": ["query"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("read_waveform", "Read PSF ASCII signals with the fast parser. Returns stats plus a chart; with 'at' (seconds) also samples every signal at the nearest time points — use instead of hand-parsing PSF files.",
         {"type": "object", "properties": {
             "path": {"type": "string", "minLength": 1, "maxLength": 500},
             "signals": {"type": "array", "items": {"type": "string", "maxLength": 200}, "maxItems": 50},
             "at": {"type": "array", "description": "Time points in seconds to sample at (nearest point). Max 200.",
                    "items": {"type": "number"}, "maxItems": 200}},
          "required": ["path"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("check_schematic", "schCheck plus dbSave on one cellview. Returns scoped diagnostics.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200},
             "cell": {"type": "string", "minLength": 1, "maxLength": 200}},
          "required": ["lib", "cell"], "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": True, "openWorldHint": True}),
        ("add_part", "Place one instance in a schematic. Opens, adds, checks, saves.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200},
             "cell": {"type": "string", "minLength": 1, "maxLength": 200},
             "master_lib": {"type": "string", "minLength": 1, "maxLength": 200},
             "master_cell": {"type": "string", "minLength": 1, "maxLength": 200},
             "master_view": {"type": "string", "maxLength": 100, "default": "symbol"},
             "name": {"type": "string", "minLength": 1, "maxLength": 100},
             "x": {"type": "number"}, "y": {"type": "number"},
             "orient": {"type": "string", "maxLength": 10, "default": "R0"}},
          "required": ["lib", "cell", "master_lib", "master_cell", "name", "x", "y"],
          "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
        ("add_pin", "Create one pin at an instance terminal. Opens, adds, checks, saves.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200},
             "cell": {"type": "string", "minLength": 1, "maxLength": 200},
             "at_inst": {"type": "string", "minLength": 1, "maxLength": 100},
             "at_term": {"type": "string", "minLength": 1, "maxLength": 100},
             "name": {"type": "string", "minLength": 1, "maxLength": 100},
             "direction": {"type": "string", "enum": ["input", "output", "inputOutput"],
                           "default": "inputOutput"}},
          "required": ["lib", "cell", "at_inst", "at_term", "name"],
          "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
        ("add_wire", "Wire two instance terminals. Opens, routes, checks, saves.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200},
             "cell": {"type": "string", "minLength": 1, "maxLength": 200},
             "from_inst": {"type": "string", "minLength": 1, "maxLength": 100},
             "from_term": {"type": "string", "minLength": 1, "maxLength": 100},
             "to_inst": {"type": "string", "minLength": 1, "maxLength": 100},
             "to_term": {"type": "string", "minLength": 1, "maxLength": 100}},
          "required": ["lib", "cell", "from_inst", "from_term", "to_inst", "to_term"],
          "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
        ("build_schematic", "Build a whole schematic in one session: fresh or append, then inst/wire/pin/label ops in order. Most efficient for big circuits.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200},
             "cell": {"type": "string", "minLength": 1, "maxLength": 200},
             "fresh": {"type": "boolean", "default": True,
                       "description": "true replaces the view, false appends"},
             "ops": {"type": "array", "maxItems": 200, "items": {"type": "object"}}},
          "required": ["lib", "cell", "ops"], "additionalProperties": False},
         {"destructiveHint": True, "idempotentHint": False, "openWorldHint": True}),
        ("new_schematic", "Create a fresh empty schematic, replacing the view if present.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200},
             "cell": {"type": "string", "minLength": 1, "maxLength": 200}},
          "required": ["lib", "cell"], "additionalProperties": False},
         {"destructiveHint": True, "idempotentHint": False, "openWorldHint": True}),
        ("evaluate", "Run one SKILL expression in Virtuoso. Max 64k chars.",
         {"type": "object", "properties": {
             "expr": {"type": "string", "minLength": 1, "maxLength": 64000},
             "timeout": {"type": "integer", "minimum": 5, "maximum": 600, "default": 60}},
          "required": ["expr"], "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
        ("lookup_skill", "Find SKILL functions by name fragment.",
         {"type": "object", "properties": {
             "query": {"type": "string", "minLength": 2, "maxLength": 200}},
          "required": ["query"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("describe_skill", "Show docs for one SKILL function.",
         {"type": "object", "properties": {
             "func": {"type": "string", "minLength": 1, "maxLength": 200}},
          "required": ["func"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("load_file", "Load one local .il file into Virtuoso.",
         {"type": "object", "properties": {
             "file": {"type": "string", "minLength": 1, "maxLength": 500},
             "timeout": {"type": "integer", "minimum": 5, "maximum": 600, "default": 60}},
          "required": ["file"], "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
        ("signoff", "Build a signoff website: shot gallery, sim results, stats. Scope it: pass shots (files for this circuit) and wave (this circuit's wave JSON). For an optional diagram, follow the signoff-flow prompt and install the external Anatomy skill; this repository does not bundle it.",
         {"type": "object", "properties": {
             "out": {"type": "string", "maxLength": 500, "default": "preview/signoff",
                     "description": "Local dir for the site"},
             "title": {"type": "string", "maxLength": 200, "default": "Virtuoso Bridge — Signoff"},
             "shots": {"type": "array", "description": "Shot files for THIS circuit only. Omit for all.",
                       "items": {"type": "string", "maxLength": 200}},
             "wave": {"type": "string", "maxLength": 500,
                      "description": "Wave JSON for this circuit's stats table"}},
          "required": [], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("log_note", "Append one exact error or note to the signoff log. Shown verbatim on next signoff site.",
         {"type": "object", "properties": {
             "message": {"type": "string", "minLength": 1, "maxLength": 4000},
             "level": {"type": "string", "enum": ["info", "warning", "error"], "default": "info"}},
          "required": ["message"], "additionalProperties": False},
         {"readOnlyHint": False, "idempotentHint": False, "openWorldHint": False}),
        ("capture_session", "Dump focused Maestro or schematic session to a local dir. Long job.",
         {"type": "object", "properties": {
             "out": {"type": "string", "minLength": 1, "maxLength": 500},
             "history": {"type": "string", "maxLength": 200}},
          "required": ["out"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("screenshot", "Capture a Virtuoso window to PNG. Returns shot handle plus file path. Use this instead of hand-rolled geOpen/hiWindowSaveImage SKILL.",
         {"type": "object", "properties": {
             "target": {"type": "string", "maxLength": 60, "default": "current",
                        "description": "ciw, current, or window number"},
             "out": {"type": "string", "maxLength": 500, "default": "preview/shots",
                     "description": "Local dir for the PNG"}},
          "required": [], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("simulate_many", "Run a fixed batch of netlists in parallel. Long job.",
         {"type": "object", "properties": {
             "netlists": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
             "max_workers": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
             "mode": {"type": "string", "enum": ["spectre", "aps", "ax", "mx", "cx", "lx"], "default": "ax"}},
          "required": ["netlists"], "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
        ("simulate", "Run one .scs netlist. Defaults: ax mode, 3600 s timeout. Long job.",
         {"type": "object", "properties": {
             "netlist": {"type": "string", "minLength": 1, "maxLength": 500},
             "mode": {"type": "string", "enum": ["spectre", "aps", "ax", "mx", "cx", "lx"], "default": "ax"},
             "timeout": {"type": "integer", "minimum": 60, "maximum": 7200, "default": 3600}},
          "required": ["netlist"], "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
    ]
    t.sort(key=lambda x: x[0])
    return [{"name": n, "title": n.replace("_", " "),
             "description": d, "inputSchema": s, "annotations": a} for n, d, s, a in t]


TOOL_NAMES = [d["name"] for d in _tool_defs()]
LONG_JOBS = {"simulate", "simulate_many", "run_test", "capture_session",
             "update_search", "startup"}

# ---------------------------------------------------------------- tasks
# Background jobs for the tasks extension. Handles are opaque UUIDs with
# a 24 h TTL. State is keyed by handle only, never by connection.

_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="vb-mcp")


def _task_create(kind: str, fn, poll_ms: int = 2000) -> dict:
    tid = "tsk_" + uuid.uuid4().hex[:16]
    now = time.time()
    with _TASKS_LOCK:
        _TASKS[tid] = {"taskId": tid, "status": "working", "kind": kind,
                       "statusMessage": kind + " started",
                       "created": now, "ttlMs": 24 * 3600 * 1000,
                       "pollIntervalMs": poll_ms, "result": None, "error": None}
    fut = _POOL.submit(_task_wrap, tid, fn)
    _ = fut
    return {"resultType": "task", "taskId": tid, "status": "working",
            "statusMessage": kind + " started",
            "ttlMs": 24 * 3600 * 1000, "pollIntervalMs": poll_ms,
            "_meta": {META_SERVER: SERVER_INFO}}


def _task_wrap(tid: str, fn):
    try:
        res = fn()
        with _TASKS_LOCK:
            st = _TASKS.get(tid)
            if st:
                st["status"] = "completed"
                st["statusMessage"] = "done"
                st["result"] = res
    except Exception as exc:
        logger.warning("task %s failed: %s", tid, exc)
        with _TASKS_LOCK:
            st = _TASKS.get(tid)
            if st:
                st["status"] = "failed"
                st["statusMessage"] = str(exc)[:300]
                st["error"] = {"code": -32603, "message": str(exc)[:1000]}


def _task_get(tid: str) -> dict:
    with _TASKS_LOCK:
        st = _TASKS.get(tid)
        if st is None:
            return {"error": _rpc_error(-32602, "unknown taskId")}
        if time.time() - st["created"] > 24 * 3600:
            return {"error": _rpc_error(-32602, "task expired, start a new job")}
        out: dict = {"resultType": "complete", "taskId": tid, "status": st["status"]}
        if st.get("statusMessage"):
            out["statusMessage"] = st["statusMessage"]
        out["ttlMs"] = st["ttlMs"]
        out["pollIntervalMs"] = st["pollIntervalMs"]
        if st["status"] == "completed":
            out["result"] = st["result"]
        elif st["status"] == "failed":
            out["error"] = st["error"]
        out["_meta"] = {META_SERVER: SERVER_INFO}
        return {"result": out}


# ---------------------------------------------------------------- tool impl
# Each impl returns a *result payload* (no resultType). The dispatcher adds
# resultType, _meta, cache fields. Long jobs run sync here; the dispatcher
# moves them into tasks when the client opts in.

# --- shared Virtuoso client (one TCP session per process) ---
# from_env() costs a handshake per call. Cache it for 10 min and retry
# once on transport failure. Saves ~50-200 ms per SKILL call and survives
# daemon restarts without agent-visible errors.
_CLIENT: dict = {"obj": None, "at": 0.0}
_CLIENT_TTL = 600.0
_CLIENT_LOCK = threading.Lock()


def _client():
    from virtuoso_bridge import VirtuosoClient
    now = time.monotonic()
    with _CLIENT_LOCK:
        obj = _CLIENT["obj"]
        if obj is not None and now - _CLIENT["at"] < _CLIENT_TTL:
            return obj
        _CLIENT["obj"] = obj = VirtuosoClient.from_env()
        _CLIENT["at"] = now
        return obj


def _client_drop():
    with _CLIENT_LOCK:
        _CLIENT["obj"] = None
        _CLIENT["at"] = 0.0


def _eval_skill(expr: str, timeout: int):
    try:
        return _client().execute_skill(expr, timeout=timeout)
    except Exception:
        _client_drop()  # stale session: reconnect once, then report
        return _client().execute_skill(expr, timeout=timeout)


# --- license probe cache (60 s): lmstat scans are slow, never per-call ---
_STATUS: dict = {"at": 0.0, "info": None}
_STATUS_TTL = 60.0


def _license_info() -> dict:
    from virtuoso_bridge.spectre.runner import SpectreSimulator
    now = time.monotonic()
    if _STATUS["info"] is not None and now - _STATUS["at"] < _STATUS_TTL:
        return _STATUS["info"]
    info = SpectreSimulator.from_env().check_license()
    _STATUS["info"] = info
    _STATUS["at"] = now
    return info

def _impl_skill_eval(a: dict) -> dict:
    r = _eval_skill(a["expr"], timeout=int(a.get("timeout", TIMEOUT_SKILL)))
    return _text_result("status=%s output=%s" % (r.status, _redact(str(r.output))),
                        {"status": str(r.status), "output": str(r.output)[:8000]})


def _impl_skill_load(a: dict) -> dict:
    p = _safe_path(a["file"])
    c = _client()
    r = c.load_il(str(p), timeout=int(a.get("timeout", TIMEOUT_SKILL)))
    return _text_result("status=%s output=%s" % (r.status, _redact(str(r.output))),
                        {"status": str(r.status)})


def _impl_bridge_status(a: dict) -> dict:
    info = _license_info()
    return _text_result(json.dumps(info, indent=2, default=str), info)


def _virtuoso_running() -> bool:
    """True when a Virtuoso process is alive."""
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-f", "dfII/bin.*virtuoso"], capture_output=True,
                           text=True, timeout=10)
    except Exception:
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _daemon_port_open(port: int = 65432) -> bool:
    """True when something answers on the daemon port."""
    import socket
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _display_ok(display: str) -> bool:
    """True when the X socket for this DISPLAY exists."""
    import re
    m = re.search(r":(\d+)", display or "")
    if not m:
        return False
    return Path("/tmp/.X11-unix/X%s" % m.group(1)).exists()


def _launch_virtuoso(display: str, workdir: Path, log: Path) -> dict:
    """Start Virtuoso detached. Returns {pid} or {error}."""
    import os
    import subprocess
    import time
    for stale in (workdir / "virtuoso.log.cdslck",):
        try:
            if stale.exists():
                stale.unlink()
        except OSError:
            pass
    workdir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["DISPLAY"] = display
    boot = workdir / "virtuoso_boot.log"
    script = ("source /opt/cadence/cadence_env.sh >/dev/null 2>&1; "
              "exec /opt/cadence/tools.lnx86/dfII/bin/virtuoso -log %s" % log)
    try:
        p = subprocess.Popen(["bash", "-lc", script], cwd=str(workdir), env=env,
                             stdout=open(boot, "ab"), stderr=subprocess.STDOUT,
                             start_new_session=True)
    except Exception as exc:
        return {"error": "launch failed: %s" % exc}
    return {"pid": p.pid}


def _wait_cadence(timeout_s: int) -> dict:
    """Poll until a trivial SKILL eval succeeds. Returns {ok} or {error}."""
    import time
    end = time.monotonic() + max(60, timeout_s)
    last = ""
    while time.monotonic() < end:
        try:
            r = _eval_skill("1+1", timeout=30)
            if str(getattr(r, "status", "")) == "ExecutionStatus.SUCCESS" \
                    and "2" in str(getattr(r, "output", "")):
                return {"ok": True}
            last = "%s %s" % (getattr(r, "status", "?"), getattr(r, "output", "")[:100])
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, str(exc)[:100])
            try:
                _client_drop()
            except Exception:
                pass
        time.sleep(10)
    return {"error": "virtuoso did not answer in %ds (last: %s)" % (timeout_s, last)}


def _impl_bridge_start(a: dict) -> dict:
    """Full bring-up: X display, Virtuoso, tunnel+daemon, end-to-end ping."""
    import os
    import subprocess
    stages: list[str] = []
    display = (os.environ.get("VB_DISPLAY") or ":0").strip()
    timeout_v = int(os.environ.get("VB_VIRTUOSO_TIMEOUT") or 600)

    if _virtuoso_running():
        if _daemon_port_open():
            stages.append("virtuoso: already running, daemon answers")
        else:
            return _tool_error(
                "virtuoso runs but no daemon answers on 65432 (its daemon died, "
                "e.g. a past port clash). Save work in the GUI, kill Virtuoso, "
                "clear virtuoso.log.cdslck, then call startup again.")
    else:
        if not _display_ok(display):
            return _tool_error(
                "no X display at %s (no /tmp/.X11-unix socket). Start your X "
                "server first, or set VB_DISPLAY to a live display." % display)
        cds = _env_paths("VB_CDS_LIB")
        workdir = cds[0].parent if cds else _work_dir()
        log = workdir / "virtuoso.log"
        launched = _launch_virtuoso(display, workdir, log)
        if "error" in launched:
            return _tool_error(launched["error"])
        stages.append("virtuoso: launched pid=%s log=%s" % (launched["pid"], log))
        wait = _wait_cadence(timeout_v)
        if "error" in wait:
            return _tool_error(wait["error"] + " (log: %s)" % log)
        stages.append("virtuoso: answers SKILL")

    cmd = [sys.executable, "-m", "virtuoso_bridge.cli", "start"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           cwd=str(BASE))
    except subprocess.TimeoutExpired:
        return _tool_error("daemon setup timed out after 300 s; check bridge_status")
    out = ((r.stdout or "") + (r.stderr or ""))[-4000:].strip()
    if r.returncode != 0:
        return _tool_error("daemon setup failed: %s" % (out or "exit %d" % r.returncode))
    stages.append("daemon: %s" % (out or "rc=0"))
    return _text_result("\n".join(stages), {"started": True, "stages": stages})


def _sch_session(lib: str, cell: str, fresh: bool):
    """Open a schematic editor session. Caller adds ops, exits to check+save."""
    from virtuoso_bridge.virtuoso.schematic.ops import (
        schematic_create_inst_by_master_name as _inst,
        schematic_create_pin_at_instance_term as _pin,
        schematic_create_wire_between_instance_terms as _wire,
        schematic_label_instance_term as _label,
    )
    c = _client()
    ed = c.schematic.create(lib, cell) if fresh else c.schematic.modify(lib, cell)
    return ed, _inst, _pin, _wire, _label


def _impl_schematic_create(a: dict) -> dict:
    ed, *_ = _sch_session(a["lib"], a["cell"], True)
    with ed:
        pass
    return _text_result("created %s/%s/schematic" % (a["lib"], a["cell"]),
                        {"lib": a["lib"], "cell": a["cell"]})


def _impl_schematic_add_inst(a: dict) -> dict:
    ed, _inst, _p, _w, _l = _sch_session(a["lib"], a["cell"], False)
    with ed:
        ed.add(_inst(a["master_lib"], a["master_cell"],
                     a.get("master_view", "symbol") or "symbol",
                     a["name"], float(a["x"]), float(a["y"]),
                     a.get("orient", "R0") or "R0"))
    return _text_result("placed %s in %s/%s" % (a["name"], a["lib"], a["cell"]),
                        {"inst": a["name"]})


def _impl_schematic_add_wire(a: dict) -> dict:
    ed, _i, _p, _wire, _l = _sch_session(a["lib"], a["cell"], False)
    with ed:
        ed.add(_wire(a["from_inst"], a["from_term"], a["to_inst"], a["to_term"]))
    return _text_result("wired %s.%s to %s.%s" % (
        a["from_inst"], a["from_term"], a["to_inst"], a["to_term"]), {"ok": True})


def _impl_schematic_add_pin(a: dict) -> dict:
    ed, _i, _pin, _w, _l = _sch_session(a["lib"], a["cell"], False)
    with ed:
        ed.add(_pin(a["at_inst"], a["at_term"], a["name"],
                    direction=a.get("direction", "inputOutput") or "inputOutput"))
    return _text_result("pin %s at %s.%s" % (a["name"], a["at_inst"], a["at_term"]),
                        {"pin": a["name"]})


def _impl_schematic_build(a: dict) -> dict:
    ed, _inst, _pin, _wire, _label = _sch_session(a["lib"], a["cell"], bool(a.get("fresh", True)))
    done = {"inst": 0, "wire": 0, "pin": 0, "label": 0}
    with ed:
        for op in a["ops"] or []:
            k = (op.get("op") or "").lower()
            if k == "inst":
                ed.add(_inst(op["master_lib"], op["master_cell"],
                             op.get("master_view", "symbol") or "symbol",
                             op["name"], float(op["x"]), float(op["y"]),
                             op.get("orient", "R0") or "R0"))
                done["inst"] += 1
            elif k == "wire":
                ed.add(_wire(op["from_inst"], op["from_term"], op["to_inst"], op["to_term"]))
                done["wire"] += 1
            elif k == "pin":
                ed.add(_pin(op["at_inst"], op["at_term"], op["name"],
                            direction=op.get("direction", "inputOutput") or "inputOutput"))
                done["pin"] += 1
            elif k == "label":
                ed.add(_label(op["inst"], op["term"], op["net"]))
                done["label"] += 1
            else:
                raise ValueError("bad op: %r (want inst/wire/pin/label)" % op.get("op"))
    return _text_result("built %s/%s: %s" % (a["lib"], a["cell"], done), done)


def _impl_schematic_check_save(a: dict) -> dict:
    c = _client()
    rep = c.schematic.check_and_save(a["lib"], a["cell"])
    lines = ["status=%s errors=%s warnings=%s" % (
        rep.status, rep.check_error_count, rep.check_warning_count)]
    for d in (rep.diagnostics or [])[:30]:
        lines.append("%s %s %s" % (d.severity, d.code, d.message))
    payload = {
        "status": str(rep.status), "errors": rep.check_error_count,
        "warnings": rep.check_warning_count,
        "diagnostics": [{"severity": str(d.severity), "code": str(d.code),
                          "message": str(d.message)[:500]} for d in (rep.diagnostics or [])[:30]]}
    content: list = []
    try:
        import base64
        shot = _editor_shot()
        if shot and len(shot) < 800000:
            content.append({"type": "image", "data": base64.b64encode(shot).decode(),
                            "mimeType": "image/png"})
    except Exception:
        pass
    content.append({"type": "text", "text": "\n".join(lines)})
    return _base_result({"content": content, "structuredContent": payload},
                        cache_public=False)


def _impl_maestro_read(a: dict) -> dict:
    c = _client()
    data = c.maestro.read_results(a["test"], a.get("history"))
    return _text_result(json.dumps(data, default=str, indent=2)[:TEXT_LIMIT], data)


def _impl_maestro_run(a: dict) -> dict:
    c = _client()
    test = a["test"]
    try:
        c.execute_skill('maeRunTest("%s")' % test.replace('"', ''),
                        timeout=int(a.get("timeout", 1800)))
        data = c.maestro.read_results(test)
        return _text_result("test=%s done\n%s" % (test, json.dumps(data, default=str)[:4000]), data)
    except Exception as exc:
        return _tool_error(str(exc))


def _spectre_sim(mode: str, timeout: int):
    from virtuoso_bridge.spectre.runner import SpectreSimulator, spectre_mode_args
    return SpectreSimulator.from_env(
        spectre_args=spectre_mode_args(mode), timeout=timeout,
        work_dir=Path("./output"))


def _impl_spectre_run(a: dict) -> dict:
    p = _safe_path(a["netlist"])
    if p.suffix not in (".scs", ".sp", ".spi", ".net"):
        return _tool_error("netlist must end with .scs/.sp/.spi/.net")
    sim = _spectre_sim(a.get("mode", "ax"), int(a.get("timeout", TIMEOUT_SPECTRE)))
    res = sim.run_simulation(str(p), {})
    if not res.ok:
        return _tool_error("; ".join((res.errors or ["simulation failed"])[:5]))
    meta = res.metadata or {}
    data = res.data or {}
    summary = {"ok": True, "signals": len(data), "output_dir": meta.get("output_dir"),
               "signal_lengths": {k: (len(v) if isinstance(v, list) else 1)
                                  for k, v in list(data.items())[:40]}}
    try:
        import time as _t
        fast = _fast_parser()
        if fast is not None and hasattr(fast, "measure_period"):
            tvec = data.get("time", data.get("TIME", []))
            osc = {}
            for k, v in list(data.items())[:12]:
                if isinstance(v, list) and k.lower() != "time":
                    m = fast.measure_period(tvec, v)
                    if m:
                        osc[k] = {"freq_hz": m["freq_hz"], "period_s": m["period_s"],
                                  "edges": m["edges"]}
            if osc:
                summary["osc"] = osc
        logf = BASE / "preview" / "data" / "sims.jsonl"
        logf.parent.mkdir(parents=True, exist_ok=True)
        with open(logf, "a", encoding="utf-8") as _f:
            _f.write(json.dumps({
                "ts": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
                "netlist": str(p), "mode": a.get("mode", "ax"),
                "ok": True, "signals": len(data),
                "output_dir": meta.get("output_dir"),
                "osc": summary.get("osc", {})}) + "\n")
    except Exception:
        pass
    content: list = []
    try:
        import base64
        vecs = {k: v for k, v in data.items() if isinstance(v, list)}
        if vecs:
            png = _wave_png({"time": data.get("time", data.get("TIME", [])),
                             **{k: v for k, v in vecs.items() if k.lower() != "time"}})
            if png and len(png) < 500000:
                content.append({"type": "image", "data": base64.b64encode(png).decode(),
                                "mimeType": "image/png"})
    except Exception:
        pass
    content.append({"type": "text", "text": "OK signals=%d out=%s\nUse read_waveform with signals for values."
                        % (len(data), meta.get("output_dir"))})
    try:
        odir = Path(str(meta.get("output_dir") or ""))
        tails: list[str] = []
        for lf in (odir / "logStatus", odir / "logFile"):
            if lf.is_file():
                lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
                keep = [ln for ln in lines if ln.strip()][-12:]
                if keep:
                    tails.append("== %s ==" % lf.name)
                    tails.extend(keep)
        if tails:
            content.append({"type": "text",
                            "text": "sim log tail:\n" + "\n".join(tails)[-2000:]})
            summary["log_tail"] = tails
    except Exception:
        pass
    summary["log_files"] = [str(Path(str(meta.get("output_dir") or "")) / n)
                            for n in ("logStatus", "logFile")]
    return _base_result({"content": content, "structuredContent": summary},
                        cache_public=False)


def _impl_spectre_parallel(a: dict) -> dict:
    nets = [_safe_path(n) for n in a["netlists"]]
    sim = _spectre_sim(a.get("mode", "ax"), TIMEOUT_SPECTRE)
    out = sim.run_parallel([(str(n), {}) for n in nets],
                           max_workers=int(a.get("max_workers", 4)))
    ok = sum(1 for r in out if r.ok)
    rows = []
    for n, r in zip(nets, out):
        m = r.metadata or {}
        rows.append({"netlist": n.name, "ok": bool(r.ok),
                     "signals": len(r.data or {}),
                     "output_dir": m.get("output_dir"),
                     "errors": (r.errors or [])[:2]})
    return _text_result("done %d/%d ok" % (ok, len(out)), {"ok": ok, "total": len(out), "runs": rows})


def _fast_parser():
    import importlib.util
    cand = BASE / "src" / "virtuoso_bridge" / "spectre" / "parsers_fast.py"
    if not cand.exists():
        return None
    spec = importlib.util.spec_from_file_location("vb_parsers_fast", cand)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore
    return m


def _wave_png(traces: dict[str, list], width: int = 800, height: int = 300,
              max_pts: int = 800, max_traces: int = 4) -> bytes | None:
    """Minimal RGB PNG line chart. Stdlib only (struct + zlib).

    Returns PNG bytes or None when no plottable data exists. Agents get
    a picture of every simulation result without new dependencies.
    """
    import struct
    import zlib
    names = [k for k in traces if k.lower() != "time"][:max_traces]
    t = traces.get("time") or traces.get("TIME") or []
    if not names or not t:
        return None
    n = min(len(t), max((len(traces[k]) for k in names), default=0))
    if n < 2:
        return None
    stride = max(1, (n + max_pts - 1) // max_pts)
    xs = list(range(0, n, stride))
    cols = [(37, 99, 235), (220, 38, 38), (22, 163, 74), (168, 85, 247)]
    series = []
    lo = hi = None
    for name in names:
        v = traces[name]
        ys = [v[i] for i in xs if i < len(v)
              and isinstance(v[i], (int, float)) and v[i] == v[i]]
        if len(ys) < 2:
            continue
        series.append(ys)
        lo = min(ys) if lo is None else min(lo, min(ys))
        hi = max(ys) if hi is None else max(hi, max(ys))
    if not series or lo is None or hi is None:
        return None
    if hi - lo < 1e-12:
        hi, lo = hi + 1.0, lo - 1.0
    ml, mr, mt, mb = 8, 8, 8, 8
    pw, ph = width - ml - mr, height - mt - mb
    px = bytearray([255] * (width * height * 3))

    def dot(x, y, c):
        if 0 <= x < width and 0 <= y < height:
            o = (y * width + x) * 3
            px[o:o + 3] = bytes(c)

    for x in range(ml, ml + pw):
        dot(x, mt, (200, 200, 200)), dot(x, mt + ph, (200, 200, 200))
    for y in range(mt, mt + ph):
        dot(ml, y, (200, 200, 200)), dot(ml + pw, y, (200, 200, 200))
    for s, c in zip(series, cols):
        ln = len(xs)
        pts = []
        for i, v in enumerate(s):
            x = ml + int(i * pw / max(ln - 1, 1))
            y = mt + int((1.0 - (v - lo) / (hi - lo)) * ph)
            pts.append((x, y))
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            dx, dy, steps = x1 - x0, y1 - y0, max(abs(x1 - x0), abs(y1 - y0), 1)
            for k in range(steps + 1):
                dot(x0 + dx * k // steps, y0 + dy * k // steps, c)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(px[r * width * 3:(r + 1) * width * 3])
                    for r in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------- PDK search
# Engineers find cells alone: parse cds.lib DEFINES, scan OA libs on disk,
# cache the index 1 h. Cell = subdir with data.dm. Views = its subdirs.

_PDK: dict = {"at": 0.0, "libs": []}
_PDK_TTL = 3600.0
_PDK_MAX_CELLS = 20000


def _expand_vars(text: str, base: Path) -> str:
    import os
    cds = (os.environ.get("CDS_INST_DIR") or os.environ.get("CDSHOME")
           or "/opt/cadence")
    guess_pdk = str(base.parent.parent) if (base.parent).name == "setup" else str(base.parent)
    table = {"CDS": cds, "PDK_DIR": guess_pdk,
             "HOME": os.path.expanduser("~"), "CWD": str(Path.cwd())}
    for k, v in table.items():
        text = text.replace("$" + k, v).replace("${" + k + "}", v)
    return os.path.expandvars(text)


def _parse_cds_lib(path: Path, seen: set | None = None) -> dict[str, str]:
    """Return {lib: path} from cds.lib, one SOFTINCLUDE level, no cycles."""
    out: dict[str, str] = {}
    if seen is None:
        seen = set()
    try:
        rp = path.resolve()
    except Exception:
        return out
    if rp in seen or not path.is_file():
        return out
    seen.add(rp)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "DEFINE" and len(parts) >= 3:
            out[parts[1]] = _expand_vars(parts[2], path)
        elif parts[0] == "SOFTINCLUDE" and len(parts) >= 2:
            inc = Path(_expand_vars(parts[1], path))
            if not inc.is_absolute():
                inc = path.parent / inc
            out.update(_parse_cds_lib(inc, seen))
    return out


def _oa_cells(lib: Path, cap: int = _PDK_MAX_CELLS) -> dict[str, list[str]]:
    """Map cell -> sorted views for one OA lib dir. Skips files, caps count."""
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
            kids = list(cell.iterdir())
        except OSError:
            continue
        names = [k.name for k in kids]
        if "data.dm" not in names:
            continue
        cells[cell.name] = sorted(k.name for k in kids if k.is_dir())
    return cells


def _pdk_index() -> list[dict]:
    now = time.monotonic()
    if _PDK["libs"] and now - _PDK["at"] < _PDK_TTL:
        return _PDK["libs"]
    defs: dict[str, str] = {}
    for cds in _cds_lib_files():
        defs.update(_parse_cds_lib(cds))
    roots = list(_pdk_roots())
    for _name, p in defs.items():
        q = Path(p)
        if q.is_dir():
            r = q if (q / "cdsinfo.tag").exists() or (q / "tech.db").exists() else q.parent
            if r.is_dir() and r not in roots:
                roots.append(r)
    seen: set[str] = set()
    libs: list[dict] = []
    cands: dict[str, Path] = {}
    for name, p in defs.items():
        q = Path(p)
        if q.is_dir() and ((q / "cdsinfo.tag").exists() or (q / "tech.db").exists()
                            or any((q / c / "data.dm").exists() for c in ("nmos_lvt",) )):
            cands[name] = q
    for r in roots:
        if not r.is_dir():
            continue
        for sub in ("", "libs.ref", "libs"):
            base = r / sub if sub else r
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
    for name in sorted(cands):
        if name in seen:
            continue
        seen.add(name)
        if not cands[name].is_dir():
            continue  # stale DEFINE (deleted path): hide, do not error
        libs.append({"name": name, "path": str(cands[name]), "cells": None})
    _PDK["libs"] = libs
    _PDK["at"] = now
    return libs


def _pdk_sidecar(path: Path, target: str, exts: tuple[str, ...] = ()) -> list[str]:
    """Find models/setup near a lib: walk up 3 levels for target dir."""
    here = path
    for _ in range(4):
        cand = here / target
        if cand.is_dir():
            if not exts:
                return [str(cand)]
            return sorted(str(x) for x in cand.rglob("*")
                          if x.is_file() and x.suffix.lower() in exts)[:20]
        here = here.parent
    return []


def _pdk_cells_cached(name: str) -> dict[str, list[str]]:
    for lib in _pdk_index():
        if lib["name"] == name:
            if lib["cells"] is None:
                lib["cells"] = _oa_cells(Path(lib["path"]))
            return lib["cells"]
    raise ValueError("unknown PDK lib: " + name)


def _impl_pdk_list(a: dict) -> dict:
    rows = []
    for lib in _pdk_index():
        p = Path(lib["path"])
        cells = _pdk_cells_cached(lib["name"])
        syms = sum(1 for _c, v in cells.items() if "symbol" in v)
        mods = [Path(m).name for m in _pdk_sidecar(p, "models", (".pm", ".lib", ".scs", ".model"))[:8]]
        tech = (p / "tech.db").exists()
        kind = "pdk" if (tech or mods) else ("stdcell" if len(cells) > 50 else "user")
        rows.append({"lib": lib["name"], "path": str(p), "kind": kind, "cells": len(cells),
                     "symbols": syms, "tech_db": tech,
                     "models": mods})
    return _text_result("%d PDK libs" % len(rows), {"libs": rows})


def _impl_pdk_search(a: dict) -> dict:
    q = a["query"].strip().lower()
    only = a.get("lib")
    limit = int(a.get("limit", 20))
    hits = []
    libs = [l for l in _pdk_index() if only is None or l["name"] == only]
    if only and not libs:
        return _tool_error("unknown PDK lib: " + only)
    for lib in libs:
        for cell, views in sorted(_pdk_cells_cached(lib["name"]).items()):
            if q in cell.lower():
                hits.append({"lib": lib["name"], "cell": cell, "views": views})
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
    if not hits:
        return _tool_error("no cell matches '%s'%s" % (
            a["query"], (" in " + only) if only else " in %d libs" % len(libs)))
    return _text_result("%d hits for '%s'" % (len(hits), a["query"]), {"hits": hits})


def _impl_pdk_info(a: dict) -> dict:
    name = a["lib"]
    path = None
    for lib in _pdk_index():
        if lib["name"] == name:
            path = Path(lib["path"])
            break
    if path is None:
        return _tool_error("unknown PDK lib: " + name)
    cells = _pdk_cells_cached(name)
    views: dict[str, int] = {}
    for _c, v in cells.items():
        for x in v:
            views[x] = views.get(x, 0) + 1
    mods = _pdk_sidecar(path, "models", (".pm", ".lib", ".scs", ".model"))
    drfs = _pdk_sidecar(path, "setup")
    drf = next((str(Path(d) / "display.drf") for d in drfs
                if (Path(d) / "display.drf").exists()), "")
    info = {"lib": name, "path": str(path), "cells": len(cells),
            "views": views, "tech_db": (path / "tech.db").exists(),
            "display_drf": drf,
            "models": mods, "sample_cells": sorted(cells)[:12]}
    return _text_result("%s: %d cells, views %s" % (name, len(cells), sorted(views)),
                        info)


def _editor_shot() -> bytes | None:
    """Best-effort PNG of the largest Virtuoso window. None when no
    display, no Xlib, or no window. Never raises: pictures are bonus,
    never a failure cause."""
    try:
        import os
        from io import BytesIO
        from Xlib import X, display as xdisplay
        from PIL import Image
        d = xdisplay.Display(os.environ.get("DISPLAY", ":0"))

        def walk(w):
            try:
                kids = w.query_tree().children
            except Exception:
                return []
            out = []
            for k in kids:
                out.append(k)
                out += walk(k)
            return out

        best = None
        for w in walk(d.screen().root):
            try:
                name = w.get_wm_name() or ""
            except Exception:
                continue
            if "irtuoso" not in name:
                continue
            g = w.get_geometry()
            area = g.width * g.height
            if best is None or area > best[0]:
                best = (area, w, g)
        if best is None:
            return None
        _, w, g = best
        raw = w.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xFFFFFFFF)
        img = Image.frombytes("RGB", (g.width, g.height), raw.data, "raw", "BGRX")
        if max(img.size) > 1280:
            img.thumbnail((1280, 1280))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _impl_psf_read(a: dict) -> dict:
    p = _safe_path(a["path"], must_exist=True)
    wants = a.get("signals") or []
    fast = _fast_parser()
    stats_fn = getattr(fast, "signal_stats", None) if fast is not None else None
    if fast is not None:
        if p.is_file():
            data = fast.parse_file_fast(p, want=wants or None)
        else:
            data = fast.parse_dir_fast(p)
    else:
        from virtuoso_bridge.spectre.parsers import (
            parse_spectre_psf_ascii, parse_psf_ascii_directory)
        data = (parse_spectre_psf_ascii(p).data if p.is_file()
                else parse_psf_ascii_directory(p))
    data = data or {}
    keys = sorted(data.keys())
    if wants:
        missing = [s for s in wants if s not in data]
        pick = {s: data[s] for s in wants if s in data}
        if missing:
            return _tool_error("missing signals: %s (have %d: %s...)"
                               % (missing[:5], len(keys), keys[:10]))
    else:
        pick = {k: data[k] for k in keys[:20]}
    summary = {}
    for k, v in pick.items():
        if isinstance(v, list):
            if stats_fn is not None:
                st = stats_fn(v)
            else:
                nums = [x for x in v[:100000]
                        if isinstance(x, (int, float)) and x == x]
                st = {"n": len(v), "numeric": len(nums),
                      "min": min(nums) if nums else None,
                      "max": max(nums) if nums else None}
            st["head"] = [x for x in v[:1000]
                           if isinstance(x, (int, float)) and x == x][:5] or v[:5]
            summary[k] = st
        else:
            summary[k] = {"scalar": v}
    txt = "signals=%d keys=%s" % (len(keys), keys[:20])
    payload = {"available": len(keys), "keys": keys[:100], "stats": summary}
    ats = a.get("at") or []
    if ats:
        import bisect
        tvec = data.get("time", data.get("TIME", []))
        rows: list[dict] = []
        for tt in list(ats)[:200]:
            try:
                f = float(tt)
            except (TypeError, ValueError):
                continue
            i = max(0, bisect.bisect_left(tvec, f) - 1) if tvec else 0
            row = {"t": tvec[i] if tvec else f}
            for k, v in pick.items():
                if isinstance(v, list) and k.lower() != "time" and i < len(v):
                    x = v[i]
                    row[k] = round(x, 6) if isinstance(x, float) else x
            rows.append(row)
        payload["samples"] = rows
        txt += " samples=%d" % len(rows)
    content: list = []
    vecs = {k: v for k, v in pick.items() if isinstance(v, list)}
    if vecs:
        try:
            import base64
            png = _wave_png({"time": data.get("time", data.get("TIME", [])),
                             **{k: v for k, v in vecs.items() if k.lower() != "time"}})
            if png and len(png) < 500000:
                content.append({"type": "image", "data": base64.b64encode(png).decode(),
                                "mimeType": "image/png"})
        except Exception:
            pass
    content.append({"type": "text", "text": txt + (" + chart" if len(content) else "")})
    r = _base_result({"content": content, "structuredContent": payload},
                     cache_public=False)
    return r


def _impl_netlist_check(a: dict) -> dict:
    import subprocess
    p = _safe_path(a["file"])
    script = SKILLS_DIR / "netlist" / "scripts" / "check_spectre_netlist.py"
    if not script.exists():
        return _tool_error("checker script missing in this checkout")
    r = subprocess.run([sys.executable, str(script), str(p),
                        "--mode", a.get("mode", "dut")],
                       capture_output=True, text=True, timeout=60)
    return _text_result((r.stdout or "") + (r.stderr or "") or "(no findings)")


def _index_mod():
    import importlib
    return importlib.import_module("virtuoso_bridge.unified_search")


def _impl_index_status(a: dict) -> dict:
    try:
        st = _index_mod().status()
    except Exception as exc:
        return _tool_error("index module unavailable: %s" % exc)
    return _text_result(json.dumps(st, indent=2, default=str)[:TEXT_LIMIT], st)


def _impl_index_build(a: dict) -> dict:
    try:
        man = _index_mod().build(kinds=a.get("kinds"), rebuild=bool(a.get("rebuild")))
    except Exception as exc:
        return _tool_error("index build failed: %s" % exc)
    body = {"counts": man.get("kinds", {}), "built_at": man.get("built_at")}
    return _text_result(json.dumps(body, indent=2), man)


def _impl_library_list(a: dict) -> dict:
    import re
    r = _eval_skill("out=list(nil) foreach(l ddGetLibList() out=cons(l->name out)) out",
                    timeout=TIMEOUT_SKILL)
    libs = sorted(set(re.findall(r'"([^"]+)"', str(r.output or ""))))
    return _text_result(", ".join(libs) if libs else "(no libraries)", {"libs": libs})


def _impl_cell_list(a: dict) -> dict:
    import re
    lib = a["lib"]
    r = _eval_skill(
        'lib=ddGetObj("%s") if(lib then out="" '
        'foreach(c lib~>cells vs="" '
        'foreach(v c~>views vs=strcat(vs v~>name ",")) '
        'out=strcat(out c~>name ":" vs ";")) out '
        'else "error: no lib")' % lib.replace('"', ''),
        timeout=TIMEOUT_SKILL)
    return _text_result(str(r.output)[:TEXT_LIMIT], {"lib": lib, "cells": str(r.output)[:8000]})


def _impl_find(a: dict) -> dict:
    try:
        mod = _index_mod()
    except Exception as exc:
        return _tool_error("index module unavailable: %s" % exc)
    kinds = a.get("kinds")
    try:
        if a.get("refresh"):
            mod.build(kinds=kinds)
        else:
            mod.ensure(kinds=kinds)
    except Exception as exc:
        return _tool_error("index build failed: %s" % exc)
    hits = mod.search(a["query"], kinds=kinds, limit=int(a.get("limit", 12)),
                      lib=a.get("lib"), cell=a.get("cell"))
    if not hits:
        return _text_result("(no hits)", {"hits": [], "query": a["query"]})
    lines = []
    for h in hits:
        loc = h["path"]
        if h.get("lib"):
            loc = "%s %s" % (h["lib"], h.get("cell") or h.get("view") or "")
        lines.append("[%s] %s  %s" % (h["kind"], h["title"], loc))
        if h.get("snippet"):
            lines.append("    " + h["snippet"])
    return _text_result("\n".join(lines)[:TEXT_LIMIT], {"hits": hits, "query": a["query"]})


def _impl_skill_find(a: dict) -> dict:
    try:
        mod = _index_mod()
        if not mod.available():
            mod.ensure(kinds=["skill"])
        hits = mod.search(a["query"], kinds=["skill"], limit=20)
        if hits:
            return _text_result(json.dumps(hits[:20], indent=2, default=str)[:TEXT_LIMIT],
                                {"hits": hits})
    except Exception:
        pass
    hits = _client().find_skill(a["query"])[:20]
    return _text_result(json.dumps(hits, default=str, indent=2)[:TEXT_LIMIT], {"hits": hits})


def _impl_skill_info(a: dict) -> dict:
    """Docs for one SKILL function: exact index hit first, find_skill fallback."""
    func = a["func"]
    try:
        mod = _index_mod()
        if not mod.available():
            mod.ensure(kinds=["skill"])
        for hit in mod.search(func, kinds=["skill"], limit=10):
            if (hit.get("title") or "").lower() == func.lower():
                body = "%s\n%s\n%s" % (hit["title"], hit["path"], hit.get("snippet", ""))
                return _text_result(body[:TEXT_LIMIT], {"info": hit})
    except Exception:
        pass
    try:
        hits = [h for h in _client().find_skill(func)
                if str(getattr(h, "name", h)).lower() == func.lower()]
        if hits:
            return _text_result(str(hits[0])[:TEXT_LIMIT], {"info": str(hits[0])[:8000]})
    except Exception as exc:
        return _tool_error("skill lookup failed: %s" % exc)
    return _tool_error("no SKILL entry for '%s'" % func)


def _impl_signoff(a: dict) -> dict:
    import subprocess
    out = a.get("out") or "preview/signoff"
    title = a.get("title") or "Virtuoso Bridge — Signoff"
    script = BASE / "tools" / "signoff.py"
    if not script.exists():
        return _tool_error("signoff generator missing in this checkout")
    cmd = [sys.executable, str(script), "--out", out, "--title", title,
           "--shots", ",".join(a.get("shots") or []),
           "--wave", a.get("wave") or ""]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                       cwd=str(BASE))
    if r.returncode != 0:
        return _tool_error("signoff failed: %s" % ((r.stdout or "") + (r.stderr or ""))[-800:])
    try:
        info = json.loads(r.stdout or "{}")
    except Exception:
        info = {"out": out}
    sid = "sign_" + uuid.uuid4().hex[:12]
    return _text_result("signoff %s -> %s/index.html (%d shots, %d results)" % (
        sid, out, info.get("shots", 0), info.get("results", 0)),
        {"signoff_id": sid, "site": "%s/index.html" % out,
         "shots": info.get("shots", 0), "results": info.get("results", 0),
         "stamp": info.get("stamp", "")})


def _impl_signoff_log(a: dict) -> dict:
    import time as _t
    logf = BASE / "preview" / "data" / "errors.jsonl"
    logf.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
             "level": a.get("level", "info"), "message": a["message"][:4000]}
    with open(logf, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return _text_result("logged [%s]" % entry["level"], entry)


def _impl_snapshot(a: dict) -> dict:
    import subprocess
    out = _safe_out_dir(a.get("out"), "snapshot_out")
    cmd = [sys.executable, "-m", "virtuoso_bridge.cli", "snapshot", "-o", str(out)]
    if a.get("history"):
        cmd += ["--history", a["history"]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SNAPSHOT)
    return _text_result(((r.stdout or "") + (r.stderr or ""))[-6000:] or "(empty snapshot)")


def _impl_screenshot(a: dict) -> dict:
    import subprocess
    out = _safe_out_dir(a.get("out"), "preview/shots")
    before = {p.name for p in out.glob("*.png")}
    cmd = [sys.executable, "-m", "virtuoso_bridge.cli", "screenshot",
           a.get("target") or "current", "-o", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return _tool_error("screenshot failed: %s" % (((r.stdout or "") + (r.stderr or ""))[-800:]))
    new = sorted([p for p in out.glob("*.png") if p.name not in before],
                 key=lambda p: p.stat().st_mtime)
    if not new:
        return _tool_error("no new PNG in %s" % out)
    shot = new[-1]
    sid = "shot_" + uuid.uuid4().hex[:12]
    return _text_result("shot %s -> %s" % (sid, shot),
                        {"shot_id": sid, "file": str(shot),
                         "uri": "file://%s" % shot.resolve()})


IMPL = {
    "evaluate": _impl_skill_eval, "load_file": _impl_skill_load,
    "status": _impl_bridge_status, "startup": _impl_bridge_start,
    "check_schematic": _impl_schematic_check_save,
    "test_results": _impl_maestro_read, "run_test": _impl_maestro_run,
    "simulate": _impl_spectre_run, "simulate_many": _impl_spectre_parallel,
    "read_waveform": _impl_psf_read, "check_netlist": _impl_netlist_check,
    "list_pdk": _impl_pdk_list, "search_pdk": _impl_pdk_search, "describe_pdk": _impl_pdk_info,
    "lookup_skill": _impl_skill_find, "describe_skill": _impl_skill_info,
    "capture_session": _impl_snapshot, "doc_search": None,  # wired below
    "screenshot": _impl_screenshot,
    "signoff": _impl_signoff,
    "log_note": _impl_signoff_log,
    "new_schematic": _impl_schematic_create,
    "add_part": _impl_schematic_add_inst,
    "add_wire": _impl_schematic_add_wire,
    "add_pin": _impl_schematic_add_pin,
    "build_schematic": _impl_schematic_build,
}


def _impl_doc_search(a: dict) -> dict:
    """Cadence documentation search, served by the unified index."""
    return _impl_find({"query": a["query"], "kinds": ["doc", "topic"],
                       "limit": int(a.get("limit", 10))})


IMPL["search_docs"] = _impl_doc_search
IMPL["find"] = _impl_find
IMPL["search"] = _impl_find
IMPL["cadence_search"] = _impl_find
IMPL["list_libraries"] = _impl_library_list
IMPL["list_cells"] = _impl_cell_list
IMPL["search_status"] = _impl_index_status
IMPL["update_search"] = _impl_index_build


# Old names keep working but are hidden from tools/list. Every rename
# lands here so existing agents and transcripts never break.
ALIASES = {
    "bridge_start": "startup", "bridge_status": "status",
    "cell_list": "list_cells", "doc_search": "search_docs",
    "find": "search", "cadence_search": "search",
    "index_build": "update_search",
    "index_status": "search_status", "library_list": "list_libraries",
    "maestro_read_results": "test_results", "maestro_run": "run_test",
    "netlist_check": "check_netlist", "pdk_info": "describe_pdk",
    "pdk_list": "list_pdk", "pdk_search": "search_pdk",
    "psf_read": "read_waveform", "schematic_add_inst": "add_part",
    "schematic_add_pin": "add_pin", "schematic_add_wire": "add_wire",
    "schematic_build": "build_schematic",
    "schematic_check_save": "check_schematic",
    "schematic_create": "new_schematic", "signoff_log": "log_note",
    "skill_eval": "evaluate", "skill_find": "lookup_skill",
    "skill_info": "describe_skill", "skill_load": "load_file",
    "snapshot": "capture_session", "spectre_parallel": "simulate_many",
    "spectre_run": "simulate",
}


def dispatch_tool(name: str, args: dict, params: dict) -> dict:
    name = ALIASES.get(name, name)
    if name not in IMPL:
        raise ValueError("unknown tool: " + name)
    if not _UNLIMITED:
        lim = _RATE_LIMITS.get(name)
        if lim is not None and not _rate_ok(name, limit=lim):
            return _tool_error("rate limit for %s; retry in ~60 s, or batch into "
                               "fewer calls (one evaluate can run many expressions; "
                               "one search replaces many shell lookups)" % name)
    args = args or {}
    if name in LONG_JOBS and _client_supports_tasks(params):
        snap = dict(args)
        fn = lambda: IMPL[name](snap)  # noqa: E731
        return _task_create(name, fn)
    try:
        payload = IMPL[name](args)
    except ValueError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        logger.warning("tool %s failed: %s", name, traceback.format_exc(limit=3))
        return _tool_error("%s: %s" % (type(exc).__name__, str(exc)[:500]))
    if isinstance(payload, dict) and payload.get("isError"):
        return payload  # already a complete result
    if isinstance(payload, dict) and "content" in payload:
        return payload
    return _text_result(str(payload)[:TEXT_LIMIT])


# ---------------------------------------------------------------- resources

def _resource_list() -> list[dict]:
    items = [{
        "uri": "virtuoso://status", "name": "bridge status",
        "description": "Tunnel, daemon and license state snapshot",
        "mimeType": "application/json"}]
    if SKILLS_DIR.exists():
        for d in sorted(SKILLS_DIR.iterdir()):
            f = d / "SKILL.md"
            if f.exists():
                items.append({
                    "uri": "skill://%s/SKILL.md" % d.name, "name": "%s skill" % d.name,
                    "description": "Agent skill: %s" % d.name, "mimeType": "text/markdown"})
    revf = BASE / "preview" / "signoff" / "rev.json"
    if revf.exists():
        items.append({
            "uri": "virtuoso://signoff/latest", "name": "latest signoff",
            "description": "Revision, counts and diff of newest signoff site",
            "mimeType": "application/json"})
    return items


def _resource_read(uri: str) -> dict:
    if uri == "virtuoso://signoff/latest":
        p = BASE / "preview" / "signoff" / "rev.json"
        if not p.exists():
            raise FileNotFoundError(uri)
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": p.read_text(encoding="utf-8")}]}
    if uri == "virtuoso://status":
        try:
            info = _license_info()
            return {"contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps(info, default=str, indent=2)}]}
        except Exception as exc:
            return {"contents": [{"uri": uri, "mimeType": "application/json",
                                  "text": json.dumps({"ok": False, "error": str(exc)[:300]})}]}
    if uri.startswith("skill://"):
        rel = uri[len("skill://"):]
        if ".." in rel or rel.startswith("/"):
            raise ValueError("bad skill uri")
        p = (SKILLS_DIR / rel).resolve()
        if SKILLS_DIR.resolve() not in p.parents:
            raise ValueError("skill uri escapes skills dir")
        if not p.is_file():
            raise FileNotFoundError(uri)
        if p.stat().st_size > 2_000_000:
            raise ValueError("skill file too large")
        return {"contents": [{"uri": uri, "mimeType": "text/markdown",
                              "text": p.read_text(encoding="utf-8", errors="replace")}]}
    raise FileNotFoundError(uri)


# ---------------------------------------------------------------- prompts

def _prompt_defs() -> list[dict]:
    return [
        {"name": "spectre-enob-run",
         "description": "Lean ENOB tran: ax mode, save=selected, strobeonly, noiseruns=1",
         "arguments": [{"name": "netlist", "required": True}]},
        {"name": "schematic-bringup",
         "description": "Check-save, read diagnostics, netlist, run Maestro test",
         "arguments": [{"name": "lib", "required": True},
                       {"name": "cell", "required": True}]},
        {"name": "tune-circuit",
         "description": "Black-box sizing loop with TuRBO or scipy objective",
         "arguments": [{"name": "params", "required": True}]},
        {"name": "plan-explainer",
         "description": "START of work: when user asks for ideas, projects, or circuits, offer one isometric explainer. Ask first; install the external Anatomy skill only if accepted.",
         "arguments": [{"name": "topic", "required": True}]},
        {"name": "result-explainer",
         "description": "END of work: offer one isometric recap figure. Ask first; install the external Anatomy skill only if accepted.",
         "arguments": [{"name": "topic", "required": True}]},
        {"name": "signoff-flow",
         "description": "Build a signoff site with real screenshots and measured numbers. Install the external Anatomy skill for an optional circuit diagram.",
         "arguments": [{"name": "out", "required": True},
                       {"name": "title", "required": True}]},
    ]


def _prompt_get(name: str, args: dict) -> dict:
    args = args or {}
    if name == "spectre-enob-run":
        n = args.get("netlist", "tb.scs")
        text = ("Run lean ENOB tran:\n1. call simulate netlist=%s mode=ax timeout=3600\n"
                "2. keep save=selected with <=20 signals, strobeoutput=strobeonly\n"
                "3. set noiseruns=1 unless ensemble is needed\n"
                "4. read with read_waveform signals=[...]" % n)
    elif name == "schematic-bringup":
        text = ("Bring up %s/%s:\n1. check_schematic\n2. fix PDK callbacks\n"
                "3. run_test then test_results" % (args.get("lib", "?"), args.get("cell", "?")))
    elif name == "tune-circuit":
        text = ("Tune %s:\n1. define bounds and objective returning scalar\n"
                "2. penalty 1e6 on failure, never nan\n3. use TuRBO for 3-20 params" % args.get("params", "?"))
    elif name == "plan-explainer":
        text = ("EXPLAINER RULE — plan start for %s:\n"
                "1. user asked for ideas, a project, or circuits, or you start a plan: "
                "OFFER one isometric explainer first\n"
                "2. ASK before creating: 'Want a visual explainer of this plan?' "
                "Proceed only on yes\n"
                "3. if accepted, install Anatomy from https://github.com/wheresryan22/anatomy "
                "with npx skills add wheresryan22/anatomy -g if unavailable; then follow it\n"
                "4. never build explainers unasked, never on every message\n"
                "5. WORDS: keep technical meaning, write ASD-STE100: "
                "max 20 words per sentence, simple approved words, "
                "one instruction per sentence" % args.get("topic", "?"))
    elif name == "result-explainer":
        text = ("EXPLAINER RULE — work done for %s:\n"
                "1. all tasks finished per user: OFFER one isometric recap figure\n"
                "2. ASK before creating: 'Want a visual recap of what was built?' "
                "Proceed only on yes\n"
                "3. if accepted, install Anatomy from https://github.com/wheresryan22/anatomy "
                "with npx skills add wheresryan22/anatomy -g if unavailable; show real data\n"
                "4. never on every message, only at true end of work\n"
                "5. WORDS: keep technical meaning, write ASD-STE100: "
                "max 20 words per sentence, simple approved words, "
                "one instruction per sentence" % args.get("topic", "?"))
    elif name == "signoff-flow":
        text = ("SIGNOFF RULE for %s:\n"
                "1. install Anatomy from https://github.com/wheresryan22/anatomy "
                "with npx skills add wheresryan22/anatomy -g if unavailable; follow its process\n"
                "2. explain THIS circuit only: its blocks, its nets, its operations\n"
                "3. every number from measurement (read_waveform, sim logs), never invented\n"
                "4. every picture a real capture (screenshot) or the built figure; no stock art\n"
                "5. write the figure as anatomy.html next to the page, then build the site with "
                "signoff out=%s shots=[this circuit's PNGs] wave=[this circuit's wave JSON]\n"
                "6. resolve stale log notes with log_note before rebuild" % (
                    args.get("title", "?"), args.get("out", "?")))
    else:
        raise ValueError("unknown prompt: " + name)
    return {"description": name, "messages": [
        {"role": "user", "content": {"type": "text", "text": text}}]}


# ---------------------------------------------------------------- skills ext.

def _skill_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm: dict = {}
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip().strip('"')
    return fm


def _skill_entry(d: Path) -> dict | None:
    f = d / "SKILL.md"
    if not f.exists():
        return None
    files = [f] + sorted([p for p in d.rglob("*") if p.is_file() and p.stat().st_size < 2_000_000])[:200]
    res = []
    for p in files:
        b = p.read_bytes()
        res.append({"uri": "skill://%s/%s" % (d.name, p.relative_to(d).as_posix()),
                    "digest": "sha256:" + hashlib.sha256(b).hexdigest(),
                    "size": len(b)})
    fm = _skill_frontmatter(f)
    fm.setdefault("name", d.name)
    return {"uri": "skill://%s/SKILL.md" % d.name, "frontmatter": fm, "resources": res}


def _skills_list() -> list[dict]:
    out = []
    if SKILLS_DIR.exists():
        for d in sorted(SKILLS_DIR.iterdir()):
            if d.is_dir():
                e = _skill_entry(d)
                if e:
                    out.append(e)
    return out


# ---------------------------------------------------------------- rpc

def _reply(rid, result=None, error=None):
    msg: dict = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def _discover() -> dict:
    return _base_result({
        "protocolVersions": SUPPORTED_VERSIONS,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {},
            "prompts": {},
            "extensions": {TASKS_EXT: {}, SKILLS_EXT: {"directoryRead": False}},
        },
        "serverInfo": {**SERVER_INFO, "title": SERVER_TITLE},
    }, ttl=300000)


def handle(req: dict):
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}
    if method in ("server/discover", "initialize"):
        if method == "initialize":
            p = params or {}
            want = p.get("protocolVersion", PROTOCOL_VERSION)
            if want not in SUPPORTED_VERSIONS:
                _reply(rid, None, _rpc_error(-32022, "unsupported protocol version: " + str(want)))
                return
            # Negotiate: answer with the client's version so older
            # hosts (2024-11-05, 2025-11-25) stay connected.
            _reply(rid, _base_result({
                "protocolVersion": want,
                "capabilities": {"tools": {"listChanged": False},
                                 "resources": {}, "prompts": {}},
                "serverInfo": SERVER_INFO}))
            return
        _reply(rid, _discover())
        return
    if method in ("notifications/initialized", "notifications/cancelled",
                  "notifications/progress", "notifications/message"):
        return  # no reply for notifications
    ver = _version_of(params)
    if ver not in SUPPORTED_VERSIONS:
        if rid is not None:
            _reply(rid, None, _rpc_error(-32022, "unsupported protocol version: " + ver))
        return
    try:
        if method == "ping":
            _reply(rid, _base_result({}))
        elif method == "tools/list":
            defs = _tool_defs()
            chunk, nxt = _page(defs, (params or {}).get("cursor"), "tools")
            r = _base_result({"tools": chunk}, ttl=300000)
            if nxt:
                r["nextCursor"] = nxt
            _reply(rid, r)
        elif method == "tools/call":
            name = (params or {}).get("name", "")
            _reply(rid, dispatch_tool(name, (params or {}).get("arguments"), params))
        elif method == "resources/list":
            items = _resource_list()
            chunk, nxt = _page(items, (params or {}).get("cursor"), "resources")
            r = _base_result({"resources": chunk}, ttl=300000)
            if nxt:
                r["nextCursor"] = nxt
            _reply(rid, r)
        elif method == "resources/read":
            try:
                r = _base_result(_resource_read((params or {}).get("uri", "")),
                                 cache_public=False, ttl=60000)
            except FileNotFoundError as exc:
                _reply(rid, None, _rpc_error(-32602, "resource not found", {"uri": str(exc)}))
                return
            _reply(rid, r)
        elif method == "resources/templates/list":
            _reply(rid, _base_result({"resourceTemplates": [{
                "uriTemplate": "skill://{skill}/{path}",
                "name": "skill files",
                "description": "SKILL.md plus references for one agent skill",
                "mimeType": "text/markdown"}]}, ttl=300000))
        elif method == "prompts/list":
            _reply(rid, _base_result({"prompts": _prompt_defs()}, ttl=300000))
        elif method == "prompts/get":
            try:
                _reply(rid, _base_result(_prompt_get(
                    (params or {}).get("name", ""), (params or {}).get("arguments", {})),
                    cache_public=False))
            except ValueError as exc:
                _reply(rid, None, _rpc_error(-32602, str(exc)))
        elif method == "skills/list":
            items = _skills_list()
            chunk, nxt = _page(items, (params or {}).get("cursor"), "skills")
            r = _base_result({"skills": chunk}, ttl=300000)
            if nxt:
                r["nextCursor"] = nxt
            _reply(rid, r)
        elif method == "skills/get":
            uri = (params or {}).get("uri", "")
            name = uri[len("skill://"):].split("/")[0] if uri.startswith("skill://") else ""
            e = _skill_entry(SKILLS_DIR / name) if name else None
            if e is None:
                _reply(rid, None, _rpc_error(-32602, "unknown skill uri"))
            else:
                _reply(rid, _base_result({"skill": e}, ttl=300000))
        elif method == "tasks/get":
            g = _task_get((params or {}).get("taskId", ""))
            if "error" in g:
                _reply(rid, None, g["error"])
            else:
                _reply(rid, g["result"])
        elif method == "tasks/cancel":
            tid = (params or {}).get("taskId", "")
            with _TASKS_LOCK:
                st = _TASKS.get(tid)
                if st and st["status"] == "working":
                    st["status"] = "cancelled"
                    st["statusMessage"] = "cancel requested"
            _reply(rid, _base_result({}))
        elif method == "tasks/update":
            _reply(rid, _base_result({}))  # ack; no elicitation used yet
        elif method == "subscriptions/listen":
            _reply(rid, _base_result({"subscriptionId": 1}))
        else:
            if rid is not None:
                _reply(rid, None, _rpc_error(-32601, "no method: " + method))
    except ValueError as exc:
        if rid is not None:
            _reply(rid, None, _rpc_error(-32602, str(exc)))
    except Exception as exc:
        logger.warning("request failed: %s", traceback.format_exc(limit=3))
        if rid is not None:
            _reply(rid, None, _rpc_error(-32603, "%s: %s" % (type(exc).__name__, str(exc)[:500])))


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if isinstance(req, list):
            for r in req:
                handle(r)
        else:
            handle(req)


if __name__ == "__main__":
    main()
