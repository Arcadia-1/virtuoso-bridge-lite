#!/usr/bin/env python3
"""Execute SKILL in a running Virtuoso session via the RAMIC bridge daemon.

Zero external dependencies — uses only Python stdlib (socket, json, argparse).
Designed to run directly on the Virtuoso host or anywhere with TCP access to
the bridge daemon port.

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

# IPC protocol markers — must match core/ramic_daemon.py
STX = b'\x02'  # start-of-result (success)
NAK = b'\x15'  # start-of-result (error)


def execute(skill, host="127.0.0.1", port=65432, timeout=60):
    """Send a SKILL expression to the bridge daemon and return the result string."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.sendall(json.dumps({"skill": skill, "timeout": timeout}).encode("utf-8"))
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

    if data and data[:1] == STX:
        return data[1:].decode("utf-8", errors="replace"), None
    elif data and data[:1] == NAK:
        return None, data[1:].decode("utf-8", errors="replace")
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
    args = parser.parse_args()

    port = args.port if args.port > 0 else _default_port()

    if args.load:
        normalized = _normalize_path(args.load)
        escaped = normalized.replace('"', '\\"')
        skill = 'load("%s")' % escaped
    elif args.skill:
        skill = args.skill
    else:
        parser.error("provide a SKILL expression or use --load FILE")

    result, error = execute(skill, host=args.host, port=port, timeout=args.timeout)
    if error:
        sys.stderr.write("ERROR: %s\n" % error)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
