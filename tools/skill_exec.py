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

MAC_LEN = 64  # hex sha256 digest length
RESP_SALT = b':resp'


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


def _mac(token_bytes, message):
    return hmac.new(token_bytes, message, hashlib.sha256).hexdigest()


def execute(skill, host="127.0.0.1", port=65432, timeout=60, token=None):
    """Send a SKILL expression to the bridge daemon and return the result string."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    nonce = None
    try:
        s.connect((host, port))
        request = {"skill": skill, "timeout": timeout}
        if token:
            nonce = binascii.hexlify(os.urandom(16)).decode('ascii')
            request["nonce"] = nonce
            request["mac"] = _mac(token.encode('utf-8'), nonce.encode('ascii'))
        s.sendall(json.dumps(request).encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        return None, "timeout waiting for response"
    except ConnectionRefusedError:
        return None, "connection refused to %s:%d — is the RAMIC bridge running?" % (host, port)
    except OSError as e:
        return None, "socket error: %s" % e
    finally:
        s.close()

    if data and data[:1] == NAK:
        message = data[1:].decode("utf-8", errors="replace")
        if message.startswith("AuthError"):
            return None, message
        return None, message
    if data and data[:1] == STX:
        body = data[1:]
        if token and nonce:
            if len(body) < MAC_LEN:
                return None, (
                    "daemon did not authenticate its response — it predates "
                    "bridge token auth or runs with auth disabled; re-load "
                    "virtuoso_setup.il in the CIW"
                )
            mac, result = body[:MAC_LEN], body[MAC_LEN:]
            expected = _mac(token.encode('utf-8'), nonce.encode('ascii') + RESP_SALT)
            if not hmac.compare_digest(mac.decode('ascii', errors='replace').lower(), expected):
                return None, (
                    "daemon response failed token authentication — the service "
                    "behind the port does not hold your bridge token (another "
                    "user's daemon or a spoofed listener is bound to it)"
                )
            return result.decode("utf-8", errors="replace"), None
        if not token:
            return body.decode("utf-8", errors="replace"), None
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
        token = _load_token(args.token_file or _default_token_path())
        if not token:
            sys.stderr.write(
                "WARNING: no bridge token found (%s); sending unauthenticated "
                "request — token-secured daemons will reject it\n"
                % (args.token_file or _default_token_path())
            )

    if args.load:
        normalized = _normalize_path(args.load)
        escaped = normalized.replace('"', '\\"')
        skill = 'load("%s")' % escaped
    elif args.skill:
        skill = args.skill
    else:
        parser.error("provide a SKILL expression or use --load FILE")

    result, error = execute(skill, host=args.host, port=port, timeout=args.timeout, token=token)
    if error:
        sys.stderr.write("ERROR: %s\n" % error)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
