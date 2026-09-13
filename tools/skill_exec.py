#!/usr/bin/env python3
"""Execute SKILL in a running Virtuoso session via the RAMIC bridge daemon.

Zero external dependencies — uses only Python stdlib (socket, json, argparse,
hmac, hashlib).  Designed to run directly on the Virtuoso host or anywhere
with TCP access to the bridge daemon port.

Bridge daemons reject unauthenticated SKILL: this tool signs each request
with the shared token from ~/.virtuoso-bridge/bridge_token (override with
--token-file or RB_TOKEN_PATH) and verifies the daemon's signed response,
so a port held by another user's daemon cannot silently capture commands.

Works on Linux, macOS, and Windows (Python 3.6+).

Usage:
    python3 tools/skill_exec.py 'plus(1 2)'
    python3 tools/skill_exec.py 'hiGetCIWindow()' --port 65432
    python3 tools/skill_exec.py --load /path/to/setup.il
    python3 tools/skill_exec.py 'plus(1 2)' --timeout 120
"""
import sys
import socket
import json
import argparse
import os
import binascii
import hashlib
import hmac

# IPC protocol markers — must match src/virtuoso_bridge/virtuoso/basic/resources/ramic_bridge_daemon_3.py
STX = b'\x02'  # start-of-result (success)
NAK = b'\x15'  # start-of-result (error)

MAC_LEN = 64          # hex sha256 digest length
PROTO = 1             # wire protocol version (daemon_auth.PROTOCOL_VERSION)
REQ_DOMAIN = b'vb1-request'
RESP_DOMAIN = b'vb1-response'
HELLO_DOMAIN = b'vb1-hello'


def _frame(*parts):
    """Length-prefixed canonical byte frame (mirrors daemon_auth.py)."""
    out = b""
    for part in parts:
        if isinstance(part, str):
            part = part.encode('utf-8')
        out += str(len(part)).encode('ascii') + b':' + part
    return out


def _mac(token_bytes, *parts):
    return hmac.new(token_bytes, _frame(*parts), hashlib.sha256).hexdigest()


def _load_token(path):
    """Read a hex token from *path*; return None when unavailable."""
    if not path:
        return None
    try:
        with open(path, 'r') as handle:
            token = handle.read().strip()
    except OSError:
        return None
    if len(token) >= 32 and all(c in '0123456789abcdefABCDEF' for c in token):
        return token.lower()
    return None


def _default_token_path():
    override = os.environ.get('RB_TOKEN_PATH', '').strip()
    if override:
        return override
    home = os.path.expanduser('~')
    return os.path.join(home, '.virtuoso-bridge', 'bridge_token')


def _exchange(payload, host, port, timeout):
    """Send one JSON request; return (raw_bytes, None) or (None, error)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(json.dumps(payload).encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return data, None
    except socket.timeout:
        return None, "timeout waiting for response"
    except ConnectionRefusedError:
        return None, "connection refused to %s:%d — is the RAMIC bridge running?" % (host, port)
    except OSError as e:
        return None, "socket error: %s" % e
    finally:
        s.close()


def _verify_response(data, token, nonce):
    """Authenticate a signed response; return (marker, body) or (None, error)."""
    if len(data) < 1 + MAC_LEN:
        return None, (
            "daemon did not authenticate its response — it predates "
            "bridge token auth or runs with auth disabled; re-load "
            "virtuoso_setup.il in the CIW"
        )
    marker, mac, body = data[:1], data[1:1 + MAC_LEN], data[1 + MAC_LEN:]
    try:
        mac_ascii = mac.decode('ascii')
    except UnicodeDecodeError:
        return None, (
            "daemon did not authenticate its response — it predates "
            "bridge token auth or runs with auth disabled; re-load "
            "virtuoso_setup.il in the CIW"
        )
    if not all(c in '0123456789abcdefABCDEF' for c in mac_ascii):
        return None, (
            "daemon did not authenticate its response — it predates "
            "bridge token auth or runs with auth disabled; re-load "
            "virtuoso_setup.il in the CIW"
        )
    expected = _mac(token.encode('utf-8'), RESP_DOMAIN, nonce, marker, body)
    if not hmac.compare_digest(mac_ascii.lower(), expected):
        return None, (
            "daemon response failed token authentication — the service "
            "behind the port does not hold your bridge token (another "
            "user's daemon or a spoofed listener is bound to it)"
        )
    return marker, body


def _validate_caps(caps, token):
    """Return None for a usable handshake payload, otherwise an error."""
    if not isinstance(caps, dict):
        return "daemon handshake payload is malformed (expected a JSON object)"
    proto = caps.get("proto")
    if not isinstance(proto, int) or isinstance(proto, bool) or proto != PROTO:
        return "bridge protocol version mismatch: daemon speaks %r, client speaks v%d" % (
            proto, PROTO,
        )
    auth = caps.get("auth")
    expected_auth = "on" if token else "off"
    if auth != expected_auth:
        return "daemon handshake authentication mode mismatch: expected %s, got %r" % (
            expected_auth, auth,
        )
    pid = caps.get("virtuoso_pid")
    if pid is not None and (
        not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
    ):
        return "daemon handshake payload is malformed (bad virtuoso_pid)"
    return None


def handshake(host, port, timeout, token):
    """Side-effect-free capability probe (op=hello, no skill field).

    Returns (caps_dict, None) on success or (None, error).  Because the
    request carries nothing executable, an auth/protocol mismatch is
    discovered before any SKILL can run on the wrong daemon.
    """
    nonce = binascii.hexlify(os.urandom(16)).decode('ascii')
    request = {"proto": PROTO, "nonce": nonce, "op": "hello"}
    if token:
        request["mac"] = _mac(token.encode('utf-8'), HELLO_DOMAIN, str(PROTO), nonce)
    data, error = _exchange(request, host, port, timeout)
    if error:
        return None, error
    if data[:1] == NAK:
        message = data[1:].decode("utf-8", errors="replace").strip()
        if message.startswith("AuthError"):
            return None, message
        return None, (
            "daemon did not answer the bridge capability handshake — it "
            "predates bridge token auth; re-load virtuoso_setup.il in the CIW"
        )
    if data[:1] != STX:
        return None, "no bridge capability handshake answer from %s:%d" % (host, port)
    if token:
        marker, body = _verify_response(data, token, nonce)
        if marker is None:
            return None, body
    else:
        body = data[1:]  # unsigned reply: strip the marker before parsing
    try:
        caps = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return None, (
            "daemon handshake payload was not valid JSON — it predates "
            "bridge token auth; re-load virtuoso_setup.il in the CIW"
        )
    error = _validate_caps(caps, token)
    if error:
        return None, error
    return caps, None


def execute(skill, host="127.0.0.1", port=65432, timeout=60, token=None,
            caps=None):
    """Send a SKILL expression to the bridge daemon and return the result string.

    The capability handshake runs automatically unless a validated *caps*
    payload is supplied (as :func:`main` does after its own pre-flight) —
    ``execute()`` can never send SKILL to a daemon whose protocol/auth state
    was not checked first.
    """
    if caps is None:
        caps, error = handshake(host, port, min(timeout, 10), token)
        if error:
            return None, error
    else:
        error = _validate_caps(caps, token)
        if error:
            return None, error
    request = {"proto": PROTO, "skill": skill, "timeout": timeout}
    nonce = None
    if token:
        nonce = binascii.hexlify(os.urandom(16)).decode('ascii')
        request["nonce"] = nonce
        request["mac"] = _mac(
            token.encode('utf-8'), REQ_DOMAIN, str(PROTO), nonce,
            '%.6f' % float(timeout), skill,
        )
    data, error = _exchange(request, host, port, timeout)
    if error:
        return None, error

    if data and data[:1] == NAK:
        return None, data[1:].decode("utf-8", errors="replace").strip()
    if data and data[:1] == STX:
        if token and nonce:
            marker, body = _verify_response(data, token, nonce)
            if marker is None:
                return None, body
            return body.decode("utf-8", errors="replace"), None
        if not token:
            return data[1:].decode("utf-8", errors="replace"), None
    return None, "no response from bridge"


def _default_port():
    """Read port from environment if available, otherwise 65432."""
    for var in ("RB_PORT", "VB_REMOTE_PORT", "VB_LOCAL_PORT"):
        val = os.environ.get(var, "").strip()
        if val.isdigit():
            return int(val)
    return 65432


def _normalize_path(path):
    """Normalize a file path for SKILL load() across platforms.

    SKILL load() on Linux/macOS expects forward slashes.
    On Windows, convert backslashes to forward slashes so the
    expression works when sent to a remote Linux Virtuoso host.
    """
    return path.replace("\\", "/")


def main():
    parser = argparse.ArgumentParser(
        description="Execute SKILL in Virtuoso via the RAMIC bridge daemon.")
    parser.add_argument("skill", nargs="?",
                        help="SKILL expression to evaluate")
    parser.add_argument("--load", metavar="FILE",
                        help="Load a SKILL file instead of evaluating an expression")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bridge daemon host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0,
                        help="Bridge daemon port (default: from RB_PORT env or 65432)")
    parser.add_argument("-t", "--timeout", type=int, default=60,
                        help="Timeout in seconds (default: 60)")
    parser.add_argument("--token-file", default=None,
                        help="Bridge token file (default: RB_TOKEN_PATH or "
                             "~/.virtuoso-bridge/bridge_token; --no-token to skip auth)")
    parser.add_argument("--no-token", action="store_true",
                        help="Send an unauthenticated request (rejected by "
                             "token-secured daemons)")
    args = parser.parse_args()

    port = args.port if args.port > 0 else _default_port()

    token = None
    if not args.no_token:
        selected_token_path = args.token_file or _default_token_path()
        token = _load_token(selected_token_path)
        if not token:
            sys.stderr.write(
                "ERROR: no valid bridge token found at %s; refusing to send "
                "SKILL unauthenticated. Use --no-token only for a daemon "
                "explicitly started with authentication disabled.\n"
                % selected_token_path
            )
            return 1

    if args.load:
        normalized = _normalize_path(args.load)
        escaped = normalized.replace('"', '\\"')
        skill = 'load("%s")' % escaped
    elif args.skill:
        skill = args.skill
    else:
        parser.error("provide a SKILL expression or use --load FILE")

    # Capability handshake first: refuse to send anything executable when
    # the daemon's protocol or auth state does not match ours.
    caps, error = handshake(args.host, port, min(args.timeout, 10), token)
    if error:
        sys.stderr.write("ERROR: %s\n" % error)
        return 1

    result, error = execute(skill, host=args.host, port=port, timeout=args.timeout,
                            token=token, caps=caps)
    if error:
        sys.stderr.write("ERROR: %s\n" % error)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
