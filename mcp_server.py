#!/usr/bin/env python3
"""Optional virtuoso-bridge MCP adapter — 2026-07-28 core + skills extension.

Uses the installed virtuoso-bridge package. No extra MCP dependency. Works on Python >= 3.9.

Transports: stdio (JSON-RPC 2.0, one message per line).
Modern clients use ``server/discover``. Legacy clients use ``initialize``.
Both are supported. Every request SHOULD carry ``params._meta`` with
``io.modelcontextprotocol/protocolVersion``. Every result carries
``resultType: complete`` and ``_meta.serverInfo``.

Tools call the existing bridge APIs synchronously with bounded timeouts.
The adapter does not implement a separate job or recovery engine.

Logs go to stderr only. Never to stdout.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

from virtuoso_bridge import VirtuosoClient


class _AdapterClient(VirtuosoClient):
    """Use bridge execution without automatic transport replay for any tool."""

    def execute_skill(self, skill_code, timeout=None, *, retry_connect=False):
        return super().execute_skill(skill_code, timeout=timeout, retry_connect=False)


logger = logging.getLogger("virtuoso-bridge-mcp")
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

BASE = Path(__file__).resolve().parent
SKILLS_DIR = BASE / "skills"


def _load_bridge_env() -> None:
    """Use the same configuration discovery and parser as the bridge CLI."""
    from virtuoso_bridge.env import load_vb_env
    load_vb_env()


PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2024-11-05"]
_LEGACY_VERSION = None
SERVER_TITLE = "Virtuoso Bridge — Cadence Virtuoso + Spectre"
SERVER_INFO = {"name": "virtuoso-bridge", "version": "1.1.0",
               "title": SERVER_TITLE}

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
        ("startup", "Set up the configured bridge using virtuoso-bridge start. Honors existing local/remote configuration. Returns any CIW load instructions; does not launch or kill Virtuoso, remove locks, or prove SKILL readiness.",
         {"type": "object", "properties": {}, "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
        ("search_docs", "Search installed Cadence documentation using the bridge configuration and existing local/remote search API.",
         {"type": "object", "properties": {
             "query": {"type": "string", "minLength": 2, "maxLength": 300},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
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
        ("test_results", "Read scalar results from an explicit Maestro session and history. For waveform values use read_waveform.",
         {"type": "object", "properties": {
             "session": {"type": "string", "minLength": 1, "maxLength": 200},
             "history": {"type": "string", "minLength": 1, "maxLength": 200}},
          "required": ["session", "history"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("run_test", "Run the enabled tests in an explicit Maestro session and wait for confirmed completion before reading its acknowledged history. For .scs netlists use simulate.",
         {"type": "object", "properties": {
             "session": {"type": "string", "minLength": 1, "maxLength": 200},
             "timeout": {"type": "integer", "minimum": 60, "maximum": 7200, "default": 1800}},
          "required": ["session"], "additionalProperties": False},
         {"destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
        ("check_netlist", "Run semantic netlist checker in dut or tb mode.",
         {"type": "object", "properties": {
             "file": {"type": "string", "minLength": 1, "maxLength": 500},
             "mode": {"type": "string", "enum": ["dut", "tb"], "default": "dut"}},
          "required": ["file"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("describe_pdk", "Inspect one server-local filesystem PDK library: path, cell and view counts, techfile, models, sample cells. Does not query the active Virtuoso session or remote hosts.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200}},
          "required": ["lib"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("list_pdk", "List PDK libraries found on this MCP server's local filesystem. A remote-only PDK may be absent; use list_libraries for the running Virtuoso session. Cached.",
         {"type": "object", "properties": {}, "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("search_pdk", "Find cells by name across server-local filesystem PDK libraries. Returns lib, cell, views; remote-only libraries are not indexed.",
         {"type": "object", "properties": {
             "query": {"type": "string", "minLength": 2, "maxLength": 200},
             "lib": {"type": "string", "maxLength": 200},
             "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
          "required": ["query"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("read_waveform", "Read PSF ASCII signals with the existing bridge parser. Returns stats plus a chart; with 'at' (seconds) also samples every signal at the nearest time points.",
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
                       "description": "true creates a new view; false modifies it. Existing views require replace=true for fresh creation."},
             "replace": {"type": "boolean", "default": False, "description": "Explicit authorization to replace an existing view when fresh=true."},
             "ops": {"type": "array", "maxItems": 200, "items": {"type": "object"}}},
          "required": ["lib", "cell", "ops"], "additionalProperties": False},
         {"destructiveHint": True, "idempotentHint": False, "openWorldHint": True}),
        ("new_schematic", "Create an empty schematic. Refuses an existing view unless replace=true is explicitly authorized.",
         {"type": "object", "properties": {
             "lib": {"type": "string", "minLength": 1, "maxLength": 200},
             "cell": {"type": "string", "minLength": 1, "maxLength": 200},
             "replace": {"type": "boolean", "default": False}},
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
        ("capture_session", "Dump focused Maestro or schematic session to a local dir. Long job.",
         {"type": "object", "properties": {
             "out": {"type": "string", "minLength": 1, "maxLength": 500},
             "history": {"type": "string", "maxLength": 200}},
          "required": ["out"], "additionalProperties": False},
         {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}),
        ("screenshot", "Capture a Virtuoso window to PNG. Returns an inline image and saved file path. A capture is visual context, not an electrical check.",
         {"type": "object", "properties": {
             "target": {"type": "string", "maxLength": 60, "default": "current",
                        "description": "ciw, current, view name such as schematic, or window number"},
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
# ---------------------------------------------------------------- tool impl

# --- shared Virtuoso client (one TCP session per process) ---
# Retain the client, including request recovery context, for this process.
# Never rebuild it or replay a mutation as timeout recovery.
_CLIENT: dict = {"obj": None, "at": 0.0}
_CLIENT_LOCK = threading.Lock()


def _client():
    now = time.monotonic()
    with _CLIENT_LOCK:
        obj = _CLIENT["obj"]
        if obj is not None:
            return obj
        _CLIENT["obj"] = obj = _AdapterClient.from_env()
        _CLIENT["at"] = now
        return obj


def _eval_skill(expr: str, timeout: int):
    return _client().execute_skill(expr, timeout=timeout, retry_connect=False)


def _bridge_result(result) -> dict:
    """Preserve the bridge outcome, diagnostics and original request handle."""
    payload = result.model_dump(mode="json")
    if len(payload.get("output", "")) > TEXT_LIMIT:
        payload["output"] = payload["output"][:TEXT_LIMIT]
        payload["output_truncated"] = True
    # Generic result truncation must never discard recovery metadata.
    text = json.dumps(payload, default=str)
    response = _base_result({
        "content": [{"type": "text", "text": text[:TEXT_LIMIT]}],
        "structuredContent": payload,
    }, cache_public=False, ttl=0)
    if not result.ok:
        response["isError"] = True
    return response


def _exception_result(exc: Exception) -> dict:
    result = getattr(exc, "result", None)
    if result is not None and hasattr(result, "model_dump"):
        return _bridge_result(result)
    response = _tool_error("%s: %s" % (type(exc).__name__, str(exc)))
    response["structuredContent"] = {
        "outcome": "unknown", "diagnostic": str(exc),
        "guidance": "Do not resubmit automatically; reconcile the original operation.",
    }
    return response


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
    return _bridge_result(r)


def _impl_skill_load(a: dict) -> dict:
    p = _safe_path(a["file"])
    c = _client()
    r = c.load_il(str(p), timeout=int(a.get("timeout", TIMEOUT_SKILL)))
    return _bridge_result(r)


def _impl_bridge_status(a: dict) -> dict:
    result = _bridge_command("status")
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    response = _text_result(output, {"returncode": result.returncode, "output": output})
    if result.returncode:
        response["isError"] = True
    return response


def _bridge_command(command: str):
    import subprocess
    executable = str(Path(sys.executable).with_name(
        "virtuoso-bridge.exe" if sys.platform == "win32" else "virtuoso-bridge"))
    return subprocess.run([executable, command], capture_output=True,
                          text=True, timeout=300)


def _impl_bridge_start(a: dict) -> dict:
    """Delegate setup to the registered CLI, with its configured host and paths."""
    import subprocess
    try:
        result = _bridge_command("start")
    except subprocess.TimeoutExpired as exc:
        response = _exception_result(exc)
        response["content"][0]["text"] = (
            "Bridge setup wait timed out. Inspect the configured bridge status "
            "before retrying; setup may have completed.")
        return response
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode:
        return _tool_error("Bridge setup failed: " + output)
    return _text_result(output, {"setup_complete": True, "output": output,
        "guidance": "If setup printed a load() line, load it in the selected idle CIW. "
                    "Setup success alone does not confirm live SKILL readiness."})


def _sch_session(lib: str, cell: str, fresh: bool, replace: bool = False):
    """Open a schematic editor session. Caller adds ops, exits to check+save."""
    from virtuoso_bridge.virtuoso.schematic.ops import (
        schematic_create_inst_by_master_name as _inst,
        schematic_create_pin_at_instance_term as _pin,
        schematic_create_wire_between_instance_terms as _wire,
        schematic_label_instance_term as _label,
    )
    c = _client()
    ed = c.schematic.create(lib, cell) if fresh else c.schematic.modify(lib, cell)
    if fresh and not replace:
        from virtuoso_bridge.virtuoso.ops import q
        # Execute in the same batch, before the destructive open. An error
        # aborts the batch; no separate check/open round trip can race it.
        ed.add('when(ddGetObj(%s %s "schematic") '
               'error("View exists; explicit replace=true authorization required"))'
               % (q(lib), q(cell)))
    return ed, _inst, _pin, _wire, _label


def _impl_schematic_create(a: dict) -> dict:
    ed, *_ = _sch_session(a["lib"], a["cell"], True, a.get("replace") is True)
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
    ed, _inst, _pin, _wire, _label = _sch_session(a["lib"], a["cell"], a.get("fresh", True), a.get("replace") is True)
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
        "warnings": rep.check_warning_count, "checked": rep.checked,
        "saved": rep.saved, "lib": a["lib"], "cell": a["cell"],
        "request_metadata": rep.request_metadata,
        "evidence": "schCheck counts and dbSave result for this cellview; a screenshot is visual context only",
        "diagnostics": [{"severity": str(d.severity), "code": str(d.code),
                          "message": str(d.message)[:500]} for d in (rep.diagnostics or [])[:30]]}
    content: list = []
    try:
        shot = _editor_shot()
        if shot and len(shot) < 800000:
            payload["image_context"] = (
                "largest visible Virtuoso window; its cell identity is not verified")
            content.append({"type": "text", "text": "Visual context: largest Virtuoso window; "
                            "confirm its cell identity before treating it as this schematic."})
            content.append({"type": "image", "data": base64.b64encode(shot).decode(),
                            "mimeType": "image/png"})
    except Exception:
        pass
    content.append({"type": "text", "text": "\n".join(lines)})
    response = _base_result({"content": content, "structuredContent": payload},
                            cache_public=False)
    if not rep.ok:
        response["isError"] = True
        if (rep.request_metadata or {}).get("outcome") == "unknown":
            response["content"].append({"type": "text", "text":
                "Outcome unknown. Preserve the original request handle and reconcile before retrying."})
    return response


def _impl_maestro_read(a: dict) -> dict:
    if not a.get("session") or not a.get("history") or "test" in a:
        raise ValueError("test_results requires an explicit session and history, not a test name")
    data = _client().maestro.read_results(a["session"], history=a["history"])
    return _text_result(json.dumps(data, default=str, indent=2)[:TEXT_LIMIT], data)


def _impl_maestro_run(a: dict) -> dict:
    if not a.get("session") or "test" in a:
        raise ValueError("run_test requires a session and runs its enabled tests; test-name arguments are unsupported")
    c = _client()
    session = a["session"]
    history, status = c.maestro.run_and_wait(
        session=session, timeout=int(a.get("timeout", 1800)))
    history = (history or "").strip().strip('"')
    if status != "done" or not history or history == "nil":
        response = _tool_error("Maestro completion was not confirmed; inspect this run before retrying.")
        response["structuredContent"] = {"session": session, "history": history,
                                         "status": status, "outcome": "unknown"}
        return response
    try:
        data = c.maestro.read_results(session, history=history)
    except Exception as exc:
        response = _exception_result(exc)
        response.setdefault("structuredContent", {}).update(
            session=session, history=history, completion_status=status)
        return response
    return _text_result(json.dumps(data, default=str),
                        {"session": session, "history": history, "status": status,
                         "results": data})


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
        details = {"ok": False, "netlist": str(p), "mode": a.get("mode", "ax"),
                   "status": str(getattr(res.status, "value", res.status)),
                   "errors": res.errors, "warnings": res.warnings,
                   "tool_version": res.tool_version, "metadata": res.metadata}
        response = _base_result({"content": [{"type": "text", "text":
            "Spectre failed for %s: %s" % (p.name,
                "; ".join((res.errors or ["see structured diagnostics"])[:5]))}],
            "structuredContent": details}, cache_public=False, ttl=0)
        response["isError"] = True
        return response
    meta = res.metadata or {}
    data = res.data or {}
    summary = {"ok": True, "signals": len(data), "netlist": str(p),
               "mode": a.get("mode", "ax"), "output_dir": meta.get("output_dir"),
               "signal_lengths": {k: (len(v) if isinstance(v, list) else 1)
                                  for k, v in list(data.items())[:40]}}
    content: list = []
    vecs = {k: v for k, v in data.items() if isinstance(v, list)}
    if vecs:
        summary["chart"] = _append_wave_chart(content, {
            "time": data.get("time", data.get("TIME", [])),
            **{k: v for k, v in vecs.items() if k.lower() != "time"}})
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
    content: list = [{"type": "text", "text":
                      "Spectre batch: %d/%d runs succeeded" % (ok, len(out))}]
    previewed = 0
    for n, r in zip(nets, out):
        m = r.metadata or {}
        row = {"netlist": str(n), "ok": bool(r.ok),
               "signals": len(r.data or {}), "output_dir": m.get("output_dir"),
               "errors": r.errors, "warnings": r.warnings}
        if r.ok and previewed < 4 and r.data:
            previewed += 1
            content.append({"type": "text", "text": "Run: %s" % n.name})
            row["chart"] = _append_wave_chart(content, r.data)
        rows.append(row)
    if ok > previewed and previewed == 4:
        content.append({"type": "text", "text":
                        "Showing four waveform previews; inspect remaining runs by output_dir."})
    response = _base_result({"content": content,
                             "structuredContent": {"ok": ok, "total": len(out),
                                "mode": a.get("mode", "ax"), "runs": rows}},
                            cache_public=False)
    if ok != len(out):
        response["isError"] = True
    return response





def _wave_png(traces: dict[str, list], width: int = 800, height: int = 300,
              max_traces: int = 4) -> bytes | None:
    """Plot values at their real timestamps, retaining extrema in each pixel."""
    import math
    import struct
    import zlib
    names = [k for k, v in traces.items()
             if k.lower() != "time" and isinstance(v, list)]
    t = traces.get("time") or traces.get("TIME") or []
    if not names or not t:
        return None
    if len(t) < 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in t):
        return None
    if any(right < left for left, right in zip(t, t[1:])) or t[-1] <= t[0]:
        return None
    cols = [(37, 99, 235), (220, 38, 38), (22, 163, 74), (168, 85, 247)]
    series = []
    lo = hi = None
    for name in names:
        v = traces[name][:len(t)]
        ys = [x for x in v if isinstance(x, (int, float)) and math.isfinite(x)]
        if len(ys) < 2:
            continue
        series.append((name, v))
        lo = min(ys) if lo is None else min(lo, min(ys))
        hi = max(ys) if hi is None else max(hi, max(ys))
        if len(series) == max_traces:
            break
    if not series or lo is None or hi is None:
        return None
    if hi - lo < 1e-12:
        hi, lo = hi + 1.0, lo - 1.0
    ml, mr, mt, mb = 8, 8, 8, 8
    pw, ph = width - ml - mr, height - mt - mb
    if pw < 2 or ph < 2:
        return None
    px = bytearray([255] * (width * height * 3))

    def dot(x, y, c):
        if 0 <= x < width and 0 <= y < height:
            o = (y * width + x) * 3
            px[o:o + 3] = bytes(c)

    for x in range(ml, ml + pw):
        dot(x, mt, (200, 200, 200)), dot(x, mt + ph, (200, 200, 200))
    for y in range(mt, mt + ph):
        dot(ml, y, (200, 200, 200)), dot(ml + pw, y, (200, 200, 200))
    def line(x0, y0, x1, y1, color):
        dx, dy = x1 - x0, y1 - y0
        steps = max(abs(dx), abs(dy), 1)
        for k in range(steps + 1):
            dot(x0 + dx * k // steps, y0 + dy * k // steps, color)

    for (_name, values), color in zip(series, cols):
        # A pixel column stores both extremes, so narrow glitches are visible.
        # Nonfinite samples end a segment; no line is drawn through missing data.
        buckets = []
        segment = 0
        for i, value in enumerate(values):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                segment += 1
                continue
            x = ml + round((t[i] - t[0]) / (t[-1] - t[0]) * (pw - 1))
            y = mt + round((1 - (value - lo) / (hi - lo)) * (ph - 1))
            if buckets and buckets[-1][0] == segment and buckets[-1][1] == x:
                buckets[-1][2] = min(buckets[-1][2], y)
                buckets[-1][3] = max(buckets[-1][3], y)
                buckets[-1][5] = y
            else:
                buckets.append([segment, x, y, y, y, y])
        previous = None
        for item in buckets:
            segment, x, ymin, ymax, first, last = item
            line(x, ymin, x, ymax, color)
            if previous is not None and previous[0] == segment:
                line(previous[1], previous[5], x, first, color)
            previous = item

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(px[r * width * 3:(r + 1) * width * 3])
                    for r in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _label_wave_png(png: bytes, *, names: list[str], times: list,
                    value_range: list[float]) -> bytes | None:
    """Add axes and legend when the optional Pillow visual extra is installed."""
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    plot = Image.open(BytesIO(png)).convert("RGB")
    left, top, right, bottom = 82, 50, 180, 72
    canvas = Image.new("RGB", (left + plot.width + right,
                                top + plot.height + bottom), "white")
    canvas.paste(plot, (left, top))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
        title_font = ImageFont.truetype("DejaVuSans.ttf", 18)
    except OSError:
        font = title_font = ImageFont.load_default()
    draw.text((left, 14), "Waveform preview", fill=(25, 35, 50), font=title_font)
    draw.text((left + plot.width // 2 - 30, top + plot.height + 39),
              "Time (s)", fill=(40, 50, 65), font=font)
    draw.text((8, 34), "Values", fill=(40, 50, 65), font=font)
    x0, x1 = float(times[0]), float(times[-1])
    ymin, ymax = map(float, value_range)
    if ymax - ymin < 1e-12:
        ymin, ymax = ymin - 1.0, ymax + 1.0
    for i in range(5):
        fraction = i / 4
        x = left + 8 + round(fraction * (plot.width - 17))
        draw.line((x, top + plot.height - 8, x, top + plot.height - 2),
                  fill=(90, 100, 115), width=1)
        draw.text((x - 18, top + plot.height + 3),
                  "%.3g" % (x0 + fraction * (x1 - x0)),
                  fill=(40, 50, 65), font=font)
        y = top + 8 + round((1.0 - fraction) * (plot.height - 17))
        draw.line((left + 2, y, left + 8, y), fill=(90, 100, 115), width=1)
        draw.text((6, y - 7), "%.3g" % (ymin + fraction * (ymax - ymin)),
                  fill=(40, 50, 65), font=font)
    colors = [(37, 99, 235), (220, 38, 38), (22, 163, 74), (168, 85, 247)]
    for i, (name, color) in enumerate(zip(names, colors)):
        y = top + 12 + i * 27
        x = left + plot.width + 17
        draw.line((x, y + 6, x + 20, y + 6), fill=color, width=3)
        draw.text((x + 27, y), name[:18], fill=(25, 35, 50), font=font)
    draw.text((left, top + plot.height + 57),
              "Sample visualization; verify limits against numeric results.",
              fill=(95, 105, 120), font=font)
    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def _append_wave_chart(content: list, traces: dict[str, list]) -> dict:
    """Keep chart provenance and limitations adjacent to its image block."""
    import math
    times = traces.get("time") or traces.get("TIME") or []
    names = []
    plotted_values = []
    for name, vector in traces.items():
        if name.lower() == "time" or not isinstance(vector, list):
            continue
        valid = [v for v in vector[:len(times)]
                 if isinstance(v, (int, float)) and math.isfinite(v)]
        if len(valid) >= 2:
            names.append(name)
            plotted_values.extend(valid)
        if len(names) == 4:
            break
    details = {"x": "sample time (s)", "y": "shared numeric scale; units depend on signals",
               "traces": dict(zip(names, ["blue", "red", "green", "purple"])),
               "scope": "visualization of returned samples; no electrical pass/fail check"}
    try:
        png = _wave_png(traces)
    except Exception as exc:
        logger.warning("waveform plot failed: %s", exc)
        png = None
    if not png:
        details["status"] = "unavailable"
        content.append({"type": "text", "text": "Waveform chart unavailable; inspect numeric samples."})
        return details
    try:
        labeled = _label_wave_png(png, names=names, times=times,
                                  value_range=[min(plotted_values), max(plotted_values)])
    except Exception as exc:
        logger.warning("waveform labels failed: %s", exc)
        labeled = None
    if labeled:
        png = labeled
    details["labels_in_image"] = bool(labeled)
    if len(png) > 500000:
        details["status"] = "omitted: image exceeds 500 KB"
        content.append({"type": "text", "text": "Waveform chart exceeds tool image limit; inspect numeric samples."})
        return details
    details["status"] = "displayed"
    details["time_range_s"] = [times[0], times[-1]]
    details["value_range"] = [min(plotted_values), max(plotted_values)]
    legend = ", ".join("%s=%s" % (color, name)
                       for name, color in details["traces"].items())
    content.append({"type": "text", "text": "Waveform: time %.6g–%.6g s; %s. "
                    "Y range %.6g–%.6g in each signal's units on a shared scale. "
                    "Per-pixel extrema retain brief events. This plot does not establish pass/fail."
                    % (times[0], times[-1], legend,
                       details["value_range"][0], details["value_range"][1])})
    content.append({"type": "image", "data": base64.b64encode(png).decode(),
                    "mimeType": "image/png"})
    return details


# ---------------------------------------------------------------- PDK search
# Engineers find cells alone: parse cds.lib DEFINES, scan OA libs on disk,
# cache the index 1 h. Cell = subdir with data.dm. Views = its subdirs.

_PDK: dict = {"at": 0.0, "libs": []}
_PDK_TTL = 3600.0
_PDK_MAX_CELLS = 20000


def _expand_vars(text: str, base: Path) -> str:
    import os
    import re
    cds = (os.environ.get("CDS_INST_DIR") or os.environ.get("CDSHOME")
           or "/opt/cadence")
    guess_pdk = str(base.parent.parent) if (base.parent).name == "setup" else str(base.parent)
    table = {"CDS": cds, "CDS_INST_DIR": cds, "PDK_DIR": guess_pdk,
             "HOME": os.path.expanduser("~"), "CWD": str(Path.cwd())}

    def replacement(match):
        key = match.group(1) or match.group(2)
        return os.environ.get(key, table.get(key, match.group(0)))

    # Match whole names: replacing "$CDS" must not corrupt "$CDS_INST_DIR".
    value = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                   replacement, text.strip().strip('"'))
    return os.path.expanduser(value)


def _parse_cds_lib(path: Path, seen: set | None = None) -> dict[str, str]:
    """Resolve local DEFINEs and recursive INCLUDE/SOFTINCLUDE without cycles."""
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
        parts = line.split(maxsplit=2)
        keyword = parts[0].upper()
        if keyword == "DEFINE" and len(parts) >= 3:
            lib_path = Path(_expand_vars(parts[2], path))
            if not lib_path.is_absolute():
                lib_path = path.parent / lib_path
            out[parts[1]] = str(lib_path.resolve())
        elif keyword in ("INCLUDE", "SOFTINCLUDE") and len(parts) >= 2:
            inc = Path(_expand_vars(" ".join(parts[1:]), path))
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
    return _text_result("%d server-local filesystem PDK libraries (remote-only libraries excluded)" % len(rows),
                        {"scope": "server-local filesystem", "libs": rows})


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
            import math
            count = 0
            minimum = maximum = None
            for x in v:
                if not isinstance(x, (int, float)) or not math.isfinite(x):
                    continue
                count += 1
                minimum = x if minimum is None else min(minimum, x)
                maximum = x if maximum is None else max(maximum, x)
            st = {"n": len(v), "numeric": count,
                  "min": minimum, "max": maximum}
            st["head"] = [x for x in v[:1000]
                           if isinstance(x, (int, float)) and x == x][:5] or v[:5]
            summary[k] = st
        else:
            summary[k] = {"scalar": v}
    txt = "signals=%d keys=%s" % (len(keys), keys[:20])
    payload = {"source": str(p), "available": len(keys),
               "keys": keys[:100], "stats": summary}
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
            i = bisect.bisect_left(tvec, f) if tvec else 0
            if tvec:
                if i == len(tvec):
                    i -= 1
                elif i > 0 and f - tvec[i - 1] <= tvec[i] - f:
                    i -= 1
            row = {"t": tvec[i] if tvec else f}
            for k, v in pick.items():
                if isinstance(v, list) and k.lower() != "time" and i < len(v):
                    x = v[i]
                    row[k] = x
            rows.append(row)
        payload["samples"] = rows
        txt += " samples=%d" % len(rows)
    content: list = []
    vecs = {k: v for k, v in pick.items() if isinstance(v, list)}
    if vecs:
        payload["chart"] = _append_wave_chart(content, {
            "time": data.get("time", data.get("TIME", [])),
            **{k: v for k, v in vecs.items() if k.lower() != "time"}})
    content.append({"type": "text", "text": "PSF source: %s; %s" % (p, txt)})
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
    output = ((r.stdout or "") + (r.stderr or "")) or "(no findings)"
    response = _text_result(output, {"file": str(p),
                                     "mode": a.get("mode", "dut"),
                                     "exit_code": r.returncode})
    if r.returncode:
        response["isError"] = True
    return response











def _impl_library_list(a: dict) -> dict:
    import re
    r = _eval_skill("out=list(nil) foreach(l ddGetLibList() out=cons(l->name out)) out",
                    timeout=TIMEOUT_SKILL)
    if not r.ok:
        return _bridge_result(r)
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
    if not r.ok:
        return _bridge_result(r)
    return _text_result(str(r.output)[:TEXT_LIMIT], {"lib": lib, "cells": str(r.output)[:8000]})





def _impl_skill_find(a: dict) -> dict:
    hits = _client().find_skill(a['query'])[:20]
    return _text_result(json.dumps(hits, default=str)[:TEXT_LIMIT], {'hits': hits})


def _impl_skill_info(a: dict) -> dict:
    hits = _client().find_skill(a['func'])[:20]
    return _text_result(json.dumps(hits, default=str)[:TEXT_LIMIT], {'hits': hits})








def _impl_snapshot(a: dict) -> dict:
    import subprocess
    out = _safe_out_dir(a.get("out"), "snapshot_out")
    cmd = [str(Path(sys.executable).with_name(
        "virtuoso-bridge.exe" if sys.platform == "win32" else "virtuoso-bridge")),
        "snapshot", "-o", str(out)]
    if a.get("history"):
        cmd += ["--history", a["history"]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_SNAPSHOT)
    output = ((r.stdout or "") + (r.stderr or ""))[-6000:]
    if r.returncode:
        return _tool_error("snapshot failed: " + output)
    return _text_result(output or "(empty snapshot)",
                        {"output_dir": str(out), "cli_exit_code": r.returncode})


def _impl_screenshot(a: dict) -> dict:
    import re
    target = a.get("target") or "current"
    if not isinstance(target, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}", target) or ".." in target:
        raise ValueError("target must be a window number or simple view name")
    if isinstance(target, str) and target.isdecimal():
        target = int(target)
    out = _safe_out_dir(a.get("out"), "preview/shots")
    result = _client().screenshot(output=out, target=target, timeout=120)
    if not result.ok:
        return _bridge_result(result)
    shot = Path(str(result.output or ""))
    if out.resolve() not in shot.resolve().parents:
        return _tool_error("screenshot output escaped the selected directory")
    if not shot.is_file():
        return _tool_error("screenshot reported success but no local PNG was found: %s" % shot)
    png = shot.read_bytes()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return _tool_error("screenshot output is not a PNG: %s" % shot)
    payload = {"file": str(shot.resolve()),
               "target": target, "bytes": len(png),
               "evidence": "window capture only; electrical correctness requires checks and simulation"}
    content = [{"type": "text", "text": "Virtuoso %s window capture: %s" % (target, shot)}]
    if len(png) <= 2_000_000:
        content.append({"type": "image", "data": base64.b64encode(png).decode(),
                        "mimeType": "image/png"})
    else:
        payload["image_omitted"] = "PNG exceeds 2 MB tool display limit"
        content[0]["text"] += "; image exceeds 2 MB, use the saved file"
    return _base_result({"content": content, "structuredContent": payload},
                        cache_public=False)


IMPL = {
    "evaluate": _impl_skill_eval, "load_file": _impl_skill_load,
    "status": _impl_bridge_status, "startup": _impl_bridge_start,
    "check_schematic": _impl_schematic_check_save,
    "test_results": _impl_maestro_read, "run_test": _impl_maestro_run,
    "simulate": _impl_spectre_run, "simulate_many": _impl_spectre_parallel,
    "read_waveform": _impl_psf_read, "check_netlist": _impl_netlist_check,
    "list_pdk": _impl_pdk_list, "search_pdk": _impl_pdk_search, "describe_pdk": _impl_pdk_info,
    "lookup_skill": _impl_skill_find, "describe_skill": _impl_skill_info,
    "capture_session": _impl_snapshot,
    "screenshot": _impl_screenshot,
    "new_schematic": _impl_schematic_create,
    "add_part": _impl_schematic_add_inst,
    "add_wire": _impl_schematic_add_wire,
    "add_pin": _impl_schematic_add_pin,
    "build_schematic": _impl_schematic_build,
}


def _impl_doc_search(a: dict) -> dict:
    result = _client().search_docs(a['query'], limit=int(a.get('limit', 10)))
    return _text_result(json.dumps(result, default=str)[:TEXT_LIMIT], result)


IMPL["search_docs"] = _impl_doc_search
IMPL["list_libraries"] = _impl_library_list
IMPL["list_cells"] = _impl_cell_list


# Old names keep working but are hidden from tools/list. Every rename
# lands here so existing agents and transcripts never break.
ALIASES = {
    "bridge_start": "startup", "bridge_status": "status",
    "cell_list": "list_cells", "doc_search": "search_docs",
    "library_list": "list_libraries",
    "maestro_read_results": "test_results", "maestro_run": "run_test",
    "netlist_check": "check_netlist", "pdk_info": "describe_pdk",
    "pdk_list": "list_pdk", "pdk_search": "search_pdk",
    "psf_read": "read_waveform", "schematic_add_inst": "add_part",
    "schematic_add_pin": "add_pin", "schematic_add_wire": "add_wire",
    "schematic_build": "build_schematic",
    "schematic_check_save": "check_schematic",
    "schematic_create": "new_schematic",
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
                               "use search_docs for documentation)" % name)
    args = args or {}
    try:
        # Bridge helpers may print setup guidance. Keep stdout JSON-RPC only.
        with contextlib.redirect_stdout(sys.stderr):
            payload = IMPL[name](args)
    except ValueError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        logger.warning("tool %s failed: %s", name, traceback.format_exc(limit=3))
        return _exception_result(exc)
    if isinstance(payload, dict) and payload.get("isError"):
        return payload  # already a complete result
    if isinstance(payload, dict) and "content" in payload:
        return payload
    return _text_result(str(payload)[:TEXT_LIMIT])


# ---------------------------------------------------------------- resources

def _resource_list() -> list[dict]:
    items = [{
        "uri": "virtuoso://status", "name": "Spectre license status",
        "description": "Spectre license probe; use the status tool for bridge readiness",
        "mimeType": "application/json"}]
    if SKILLS_DIR.exists():
        for d in sorted(SKILLS_DIR.iterdir()):
            f = d / "SKILL.md"
            if f.exists():
                items.append({
                    "uri": "skill://%s/SKILL.md" % d.name, "name": "%s skill" % d.name,
                    "description": "Agent skill: %s" % d.name, "mimeType": "text/markdown"})
    return items


def _resource_read(uri: str) -> dict:
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
        if _LEGACY_VERSION and isinstance(result, dict):
            result = dict(result)
            for key in ("resultType", "ttlMs", "cacheScope"):
                result.pop(key, None)
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def _discover() -> dict:
    return _base_result({
        "supportedVersions": SUPPORTED_VERSIONS,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {},
            "prompts": {},
            "extensions": {SKILLS_EXT: {"directoryRead": False}},
        },
    }, ttl=300000)


def handle(req: dict):
    global _LEGACY_VERSION
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}
    ver = _version_of(params)
    if method != "initialize" and ver not in SUPPORTED_VERSIONS:
        if rid is not None:
            _reply(rid, None, _rpc_error(
                -32022, "Unsupported protocol version",
                {"supported": SUPPORTED_VERSIONS, "requested": ver}))
        return
    if method in ("server/discover", "initialize"):
        if method == "initialize":
            p = params or {}
            want = p.get("protocolVersion")
            legacy = [v for v in SUPPORTED_VERSIONS if v < "2026-07-28"]
            if want not in legacy:
                want = legacy[0]
            _LEGACY_VERSION = want
            # Legacy negotiation returns a supported fallback for unknown
            # versions; the client decides whether it can continue.
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
    _load_bridge_env()
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
