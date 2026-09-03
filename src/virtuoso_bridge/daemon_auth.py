"""Bridge daemon token authentication (SSH-bootstrapped pre-shared key).

The daemon port is host-global on shared EDA servers, so port possession
proves nothing: another user's Virtuoso (or a fake listener) can hold the
port the SSH tunnel forwards to.  Identity checks based on env vars or PIDs
are forgeable by the process behind the port.  The one thing a foreign local
user cannot obtain is a secret that only ever travelled over *our*
authenticated SSH channel and lives in *our* 0600 file.

Scheme
------
- Token: 64 hex chars, stored at ``~/.virtuoso-bridge/bridge_token`` on the
  machine running the daemon (mode 0600).  Read-or-created by both sides, so
  deployment over SSH (``SSHClient.ensure_daemon_token``) and a daemon
  started by a manual ``load(...)`` converge on the same secret.
- Client -> daemon: request JSON gains ``nonce`` (random hex) and
  ``mac = HMAC-SHA256(token, nonce)``.  The daemon executes SKILL only when
  the MAC verifies; otherwise it NAKs with an ``AuthError`` and runs nothing.
- Daemon -> client: response payload gains a 64-hex prefix
  ``HMAC-SHA256(token, nonce + ":resp")``, proving the listener holds the
  token too.  A squatter that holds the port can neither execute our SKILL
  nor forge a valid response.

The token itself never crosses the TCP wire, and HMAC proofs from one nonce
cannot be replayed for another.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

TOKEN_DIRNAME = ".virtuoso-bridge"
TOKEN_FILENAME = "bridge_token"
# Env override for the token file location ( honoured by client and daemon;
# primarily for tests and exotic homes ).
TOKEN_PATH_ENV = "VB_BRIDGE_TOKEN"

MAC_LEN = 64  # hex sha256 digest length
RESPONSE_SALT = ":resp"


class DaemonAuthError(Exception):
    """Raised when the daemon (or its response) fails token authentication."""


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


def read_local_token(path: str | Path | None = None) -> str | None:
    """Read-only variant: never creates the token file."""
    target = Path(path) if path else token_path()
    try:
        existing = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return existing.lower() if is_valid_token(existing) else None


def read_or_create_local_token(path: str | Path | None = None) -> str | None:
    """Read the local token file, creating it (mode 0600) when absent.

    Returns None when the file holds no valid token and cannot be (re)
    created — callers fall back to unauthenticated legacy behaviour.
    """
    target = Path(path) if path else token_path()
    try:
        existing = target.read_text(encoding="utf-8").strip()
        if is_valid_token(existing):
            return existing.lower()
    except OSError:
        pass
    token = generate_token()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except OSError:
        return None
    return token


def sign_request(token: str, nonce: str) -> str:
    return hmac.new(
        token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def response_mac(token: str, nonce: str) -> str:
    return hmac.new(
        token.encode("utf-8"),
        (nonce + RESPONSE_SALT).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def split_response(raw: str) -> tuple[str, str] | None:
    """Split a raw response into ``(marker, body_without_mac)``.

    Returns ``(marker, rest)`` when a hex-MAC prefix is present, or
    ``None`` when the payload has no MAC prefix (legacy / auth-disabled
    daemon).  Callers decide policy.
    """
    if not raw:
        return None
    marker, body = raw[0], raw[1:]
    if len(body) >= MAC_LEN and all(
        c in "0123456789abcdefABCDEF" for c in body[:MAC_LEN]
    ):
        return marker, body[MAC_LEN:]
    return None


def verify_response(raw: str, token: str, nonce: str) -> str:
    """Authenticate a raw daemon response; return the MAC-stripped raw.

    Raises :class:`DaemonAuthError` when the listener does not hold the
    token (squatted port / fake daemon) or when it predates token auth —
    callers translate that into an actionable error.
    """
    if not raw:
        return raw
    marker, body = raw[0], raw[1:]
    if len(body) < MAC_LEN:
        raise DaemonAuthError(
            "daemon did not authenticate its response — it predates bridge "
            "token auth or runs with auth disabled; run `virtuoso-bridge "
            "restart` (or re-load virtuoso_setup.il in the CIW) to upgrade it"
        )
    mac, rest = body[:MAC_LEN], body[MAC_LEN:]
    if not all(c in "0123456789abcdefABCDEF" for c in mac):
        raise DaemonAuthError(
            "daemon did not authenticate its response — it predates bridge "
            "token auth or runs with auth disabled; run `virtuoso-bridge "
            "restart` (or re-load virtuoso_setup.il in the CIW) to upgrade it"
        )
    expected = response_mac(token, nonce)
    if not hmac.compare_digest(mac.lower(), expected):
        raise DaemonAuthError(
            "daemon response failed token authentication — the service "
            "behind the port does not hold your bridge token (another "
            "user's daemon or a spoofed listener is bound to it)"
        )
    return marker + rest
