#!/usr/bin/env python3
"""Hello World — run after loading ramic_bridge.il in Virtuoso CIW.

Usage:
    python3 hello_virtuoso.py
"""

from virtuoso_bridge import VirtuosoClient, decode_skill_output

client = VirtuosoClient.local(port=65432)

# Print a message in the Virtuoso CIW
client.execute_skill('printf("Hello from Python!\\n")')
print("Sent 'Hello from Python!' to CIW")

# Get a return value back in Python
result = client.execute_skill("plus(1 2)")
print(f"plus(1 2) = {decode_skill_output(result.output)}")

# Get the Virtuoso version
result = client.execute_skill("getVersion()")
print(f"Virtuoso version: {decode_skill_output(result.output)}")
