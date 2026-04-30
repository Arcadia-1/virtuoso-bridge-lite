#!/usr/bin/env python3
"""Hello World client — run after loading my_bridge.il in Virtuoso CIW.

Usage:
    python3 hello_virtuoso.py
"""

import socket


def run_skill(skill_code: str, host: str = "127.0.0.1", port: int = 12345) -> str:
    """Send a SKILL expression to Virtuoso and return the result string."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((host, port))
        s.sendall(skill_code.encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk

    if not data:
        raise RuntimeError("No response from daemon")
    prefix = data[0]
    payload = data[1:].decode("utf-8", errors="replace")
    if prefix == 0x02:   # STX = success
        return payload
    elif prefix == 0x15: # NAK = error
        raise RuntimeError(f"SKILL error: {payload}")
    return payload       # fallback


if __name__ == "__main__":
    # Print a message in the Virtuoso CIW
    run_skill('printf("Hello from Python!\\n")')
    print("Sent 'Hello from Python!' to CIW")

    # Get a return value back in Python
    result = run_skill("plus(1 2)")
    print(f"plus(1 2) = {result}")

    # Get the Virtuoso version
    version = run_skill("getVersion()")
    print(f"Virtuoso version: {version}")
