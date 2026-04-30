"""Read the test/padring schematic using virtuoso-bridge-lite's built-in reader."""

import json
from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.schematic.reader import read_schematic

client = VirtuosoClient.local(port=65432)

data = read_schematic(client, "test", "padring")

# Instances
print(f"=== Instances ({len(data['instances'])}) ===")
for inst in data["instances"]:
    print(f"  {inst['name']:20s}  {inst['lib']}/{inst['cell']}")
    if inst["params"]:
        for k, v in inst["params"].items():
            print(f"    {k} = {v}")
    if inst["terms"]:
        for term, net in inst["terms"].items():
            print(f"    .{term} -> {net}")

# Nets
print(f"\n=== Nets ({len(data['nets'])}) ===")
for net_name, net in data["nets"].items():
    conns = ", ".join(net["connections"])
    print(f"  {net_name:20s}  [{conns}]")

# Pins
print(f"\n=== Pins ({len(data['pins'])}) ===")
for pin_name, pin in data["pins"].items():
    print(f"  {pin_name:20s}  {pin['direction']}")

# Dump full result as JSON for inspection
with open("padring_schematic.json", "w") as f:
    json.dump(data, f, indent=2)
print("\nFull data saved to padring_schematic.json")
