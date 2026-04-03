#!/usr/bin/env python3
"""Minimal RAMIC Bridge client — send SKILL, get result.

Usage:
    # After SSH tunnel is up:
    python core/bridge_client.py '1+2'
    python core/bridge_client.py 'hiGetCurrentWindow()'
    python core/bridge_client.py 'geGetEditCellView()~>cellName'
"""

import json
import socket
import sys


def execute_skill(skill_code, host="127.0.0.1", port=65432, timeout=30):
    """Send a SKILL expression, return the result string."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    s.sendall(json.dumps({"skill": skill_code, "timeout": timeout}).encode())
    s.shutdown(socket.SHUT_WR)
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    # First byte: \x02 = success, \x15 = error
    if data and data[0:1] == b'\x02':
        return {"ok": True, "result": data[1:].decode("utf-8", errors="replace")}
    else:
        return {"ok": False, "error": data[1:].decode("utf-8", errors="replace")}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python bridge_client.py '<SKILL expression>'")
        sys.exit(1)
    result = execute_skill(sys.argv[1])
    if result["ok"]:
        print(result["result"])
    else:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(1)
