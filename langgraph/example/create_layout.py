"""Place the padring cell instance in a new layout at (0, 0)."""

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.layout.ops import layout_create_param_inst

client = VirtuosoClient.local(port=65432)

with client.layout.edit("test", "padring") as lay:
    lay.add(layout_create_param_inst(
        "IN22FDX_GPIO18_10M3S40PI",
        "IN22FDX_GPIO18_10M3S40PI_ANA_H",
        "layout",
        "I0",
        0.0, 0.0,
        "R0",
    ))

print("Done. Open test/padring/layout in Virtuoso to verify.")
