"""Bridge daemon token authentication (SSH-bootstrapped pre-shared key).

The daemon port is host-global on shared EDA servers, so port possession
proves nothing: another user's Virtuoso (or a fake listener) can hold the
port the SSH tunnel forwards to.  Identity checks based on env vars or PIDs
are forgeable by the process behind the port.  The one thing a foreign local
user cannot obtain is a secret that only ever travelled over *our*
authenticated SSH channel and lives in *our* 0600 file.

Scheme (wire protocol v1)
-------------------------
- Token: 64 hex chars, stored atomically at
  ``~/.virtuoso-bridge/bridge_token`` (mode 0600, directory 0700) on the
  machine running the daemon.  Read-or-created by both sides, so deployment
  over SSH (``SSHClient.ensure_daemon_token``) and a daemon started by a
  manual ``load(...)`` converge on the same secret.  A missing or unusable
  token is **fatal by default** — both sides refuse to operate unless the
  insecure legacy mode is explicitly opted into (``VB_ALLOW_UNAUTHENTICATED_DAEMON=1``
  on the client, ``RB_ALLOW_UNAUTHENTICATED=1`` on the daemon).
- Every MAC is HMAC-SHA256 over a length-prefixed canonical frame, so field
  boundaries are unambiguous regardless of payload contents::

      frame(p0, p1, ...) = b"<len(p0)>:p0<len(p1)>:p1..."

  Request:  ``mac = HMAC(token, frame("vb1-request", proto, nonce,
  timeout, skill))`` — the whole request is authenticated, not just the
  nonce, so no field can be swapped in flight.
  Response: ``mac = HMAC(token, frame("vb1-response", nonce, marker,
  body))`` — the status marker *and* the full body are authenticated.
  Handshake: ``mac = HMAC(token, frame("vb1-hello", proto, nonce))``.
- The client performs a side-effect-free capability handshake (``op=hello``,
  no ``skill`` field) before its first real command: an up-to-date daemon
  answers with signed capabilities, while a pre-token daemon fails on the
  missing ``skill`` key *without executing anything* — authentication
  failure is therefore never discovered after an execution.
- Server-side replay protection: daemons remember the nonces of every
  authenticated request for a TTL exceeding any request timeout and reject
  duplicates.
- The token itself never crosses the TCP wire.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import secrets
import tempfile
from pathlib import Path

TOKEN_DIRNAME = ".virtuoso-bridge"
TOKEN_FILENAME = "bridge_token"
# Env override for the token file location ( honoured by client and daemon;
# primarily for tests and exotic homes ).
TOKEN_PATH_ENV = "VB_BRIDGE_TOKEN"
# Explicit client-side opt-in to insecure (unauthenticated) legacy mode.
# Without it, an unavailable token is fatal.
UNAUTH_OPTIN_ENV = "VB_ALLOW_UNAUTHENTICATED_DAEMON"
# The daemon-side counterpart (read by ramic_bridge_daemon_*.py).
DAEMON_UNAUTH_OPTIN_ENV = "RB_ALLOW_UNAUTHENTICATED"

MAC_LEN = 64  # hex sha256 digest length

PROTOCOL_VERSION = 1

_REQUEST_DOMAIN = "vb1-request"
_RESPONSE_DOMAIN = "vb1-response"
_HELLO_DOMAIN = "vb1-hello"

STX = "\x02"  # success marker
NAK = "\x15"  # error marker

_TRUTHY = {"1", "true", "yes", "on"}


class DaemonAuthError(Exception):
    """Raised when the daemon (or its response) fails token authentication."""


class DaemonTokenError(RuntimeError):
    """Raised when no usable bridge token exists and legacy mode was not
    explicitly opted into (see ``UNAUTH_OPTIN_ENV``)."""


def allow_unauthenticated() -> bool:
    """True only when the insecure legacy mode was explicitly opted into."""
    return os.environ.get(UNAUTH_OPTIN_ENV, "").strip().lower() in _TRUTHY


def token_path() -> Path:
    override = os.environ.get(TOKEN_PATH_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / TOKEN_DIRNAME / TOKEN_FILENAME


def is_valid_token(token: str | None) -> bool:
    return bool(token) and len(token) == MAC_LEN and all(
        c in "0123456789abcdefABCDEF" for c in token
    )


def generate_token() -> str:
    return secrets.token_hex(32)


def _tighten_perms(target: Path) -> None:
    """Best-effort 0700 directory / 0600 file (chmod is largely a no-op on
    Windows, so failures are ignored)."""
    for path, mode in ((target.parent, 0o700), (target, 0o600)):
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _chmod_dir_0700(directory: Path) -> None:
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # Windows: mode is advisory


def create_token_exclusive(target: str | Path, token: str) -> str:
    """Create the token file if absent (0600 under a 0700 dir); return the
    on-disk token.

    The file is fully written to a temp file and hard-linked into place
    (create-if-absent), so under a first-time creation race the winner's
    complete file is always the one on disk and every racer **adopts** it —
    concurrent creators converge on one secret instead of holding different
    tokens.  If the filesystem cannot provide create-if-absent hard links,
    creation fails closed rather than falling back to an overwrite-prone
    rename.  Raises OSError on failure.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _chmod_dir_0700(target.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".bridge_token.", suffix=".tmp"
    )
    try:
        try:
            os.fchmod(fd, 0o600)
        except (OSError, AttributeError):
            pass  # Windows / old Pythons: mode is advisory; directory ACL applies
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, str(target))
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
        _tighten_perms(target)
        disk = read_local_token(target)
        if disk is None:
            raise OSError(f"token file {target} unreadable after creation")
        return disk
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def read_local_token(path: str | Path | None = None) -> str | None:
    """Read-only variant: never creates the token file."""
    target = Path(path) if path else token_path()
    try:
        existing = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return existing.lower() if is_valid_token(existing) else None


def read_or_create_local_token(path: str | Path | None = None) -> str | None:
    """Read the local token file, creating it atomically (mode 0600 under a
    0700 directory) when absent.  Returns None when the file holds no valid
    token and cannot be (re)created — *callers decide policy*; user-facing
    paths must treat None as fatal (see :func:`local_token_or_raise`).
    """
    target = Path(path) if path else token_path()
    try:
        existing = target.read_text(encoding="utf-8").strip()
        if is_valid_token(existing):
            token = existing.lower()
            _tighten_perms(target)  # heal over-permissive pre-existing files
            return token
    except OSError:
        pass
    token = generate_token()
    try:
        return create_token_exclusive(target, token)
    except OSError:
        return None


def local_token_or_raise(path: str | Path | None = None) -> str | None:
    """Token for user-facing client construction: fatal by default.

    Raises :class:`DaemonTokenError` when no usable token exists, unless the
    explicit ``VB_ALLOW_UNAUTHENTICATED_DAEMON=1`` opt-in is set — in which
    case None is returned and the session runs unauthenticated (and can only
    talk to daemons that were explicitly started with auth disabled).
    """
    token = read_or_create_local_token(path)
    if token is not None:
        return token
    if allow_unauthenticated():
        return None
    raise DaemonTokenError(
        f"no usable bridge token at {Path(path) if path else token_path()} "
        f"and it could not be created (check directory permissions). The "
        f"bridge refuses to run unauthenticated by default; fix the token "
        f"file, or set {UNAUTH_OPTIN_ENV}=1 to explicitly accept "
        f"unauthenticated legacy mode."
    )


# ---------------------------------------------------------------------------
# Canonical frames and MACs
# ---------------------------------------------------------------------------


def _part(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def canonical_frame(*parts: str | bytes) -> bytes:
    """Length-prefixed concatenation — unambiguous for any payload bytes."""
    out = bytearray()
    for value in parts:
        chunk = _part(value)
        out += str(len(chunk)).encode("ascii") + b":"
        out += chunk
    return bytes(out)


def format_timeout(timeout: float) -> str:
    """Deterministic timeout rendering for the canonical frame (identical on
    Python 2 and 3, and across int/float JSON encodings)."""
    return "%.6f" % float(timeout)


def _mac_hex(token: str, *parts: str | bytes) -> str:
    return hmac.new(
        token.encode("utf-8"), canonical_frame(*parts), hashlib.sha256
    ).hexdigest()


def request_mac(
    token: str,
    *,
    nonce: str,
    skill: str,
    timeout: float,
    proto: int = PROTOCOL_VERSION,
) -> str:
    """MAC authenticating the complete request (proto, nonce, timeout, skill)."""
    return _mac_hex(
        token, _REQUEST_DOMAIN, str(int(proto)), nonce, format_timeout(timeout), skill
    )


def hello_mac(token: str, *, nonce: str, proto: int = PROTOCOL_VERSION) -> str:
    """MAC authenticating a capability-handshake (hello) request."""
    return _mac_hex(token, _HELLO_DOMAIN, str(int(proto)), nonce)


def response_mac(
    token: str,
    *,
    nonce: str,
    marker: str | bytes,
    body: str | bytes,
) -> str:
    """MAC authenticating the complete response (status marker and body)."""
    return _mac_hex(token, _RESPONSE_DOMAIN, nonce, marker, body)


_HEX = set("0123456789abcdefABCDEF")


def looks_like_hex_mac(text: str | bytes) -> bool:
    if len(text) < MAC_LEN:
        return False
    chunk = text[:MAC_LEN]
    if isinstance(chunk, bytes):
        chunk = chunk.decode("ascii", errors="ignore")
    return len(chunk) == MAC_LEN and all(c in _HEX for c in chunk)


def is_valid_nonce(nonce: str | None) -> bool:
    return bool(nonce) and 16 <= len(nonce) <= 128 and all(c in _HEX for c in nonce)


# ---------------------------------------------------------------------------
# Response verification (client side)
# ---------------------------------------------------------------------------


_LEGACY_DAEMON_MSG = (
    "daemon did not authenticate its response — it predates bridge "
    "token auth or runs with auth disabled; run `virtuoso-bridge "
    "restart` (or re-load virtuoso_setup.il in the CIW) to upgrade it"
)


def _auth_failure(message: str) -> DaemonAuthError:
    return DaemonAuthError(message)


def split_signed(raw: bytes) -> tuple[bytes, bytes, bytes] | None:
    """Split a signed wire payload into ``(marker, mac, body)``.

    Returns None when the payload has no hex-MAC prefix (legacy /
    auth-disabled daemon).  Callers decide policy.
    """
    if len(raw) < 1 + MAC_LEN:
        return None
    marker, mac, body = raw[:1], raw[1 : 1 + MAC_LEN], raw[1 + MAC_LEN :]
    if all(c in _HEX for c in mac.decode("ascii", errors="ignore")) and len(mac) == MAC_LEN:
        return marker, mac, body
    return None


def verify_response_bytes(raw: bytes, token: str, nonce: str) -> bytes:
    """Authenticate a raw daemon response; return the MAC-stripped bytes.

    Raises :class:`DaemonAuthError` when the listener does not hold the
    token (squatted port / fake daemon) or when it predates token auth.
    """
    if not raw:
        return raw
    parts = split_signed(raw)
    if parts is None:
        raise _auth_failure(_LEGACY_DAEMON_MSG)
    marker, mac, body = parts
    expected = response_mac(token, nonce=nonce, marker=marker, body=body)
    if not hmac.compare_digest(mac.decode("ascii").lower(), expected):
        raise _auth_failure(
            "daemon response failed token authentication — the service "
            "behind the port does not hold your bridge token (another "
            "user's daemon or a spoofed listener is bound to it)"
        )
    return marker + body


def verify_response(raw: str, token: str, nonce: str) -> str:
    """String wrapper around :func:`verify_response_bytes` (text payloads)."""
    return verify_response_bytes(raw.encode("utf-8", errors="ignore"), token, nonce).decode(
        "utf-8", errors="ignore"
    )
