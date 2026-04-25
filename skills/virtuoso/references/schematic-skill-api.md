# Schematic Reference

## Edit Pattern

```python
from virtuoso_bridge.virtuoso.schematic import (
    schematic_create_inst_by_master_name as inst,
    schematic_create_pin as pin,
    schematic_create_wire_between_instance_terms as wire,
    schematic_label_instance_term as label,
)

with client.schematic.edit(lib, cell) as sch:
    sch.add(inst("analogLib", "vdc", "symbol", "V0", 0, 0, "R0"))
    sch.add(inst("analogLib", "gnd", "symbol", "GND0", 0, -0.5, "R0"))
    sch.add(wire("V0", "MINUS", "GND0", "gnd!"))
    sch.add(label("V0", "PLUS", "VDD"))
    sch.add(pin("VDD", 0, 1.0, "R0", direction="inputOutput"))
    sch.add_net_label_to_transistor("M0", drain_net="OUT", gate_net="IN",
        source_net="VSS", body_net="VSS")
```

`sch.add(skill_cmd)` queues SKILL commands; `schCheck` + `dbSave` run on context exit.

## CDF Parameter Setting

Use `set_instance_params` for PDK devices — handles `schHiReplace` + CDF callback:

```python
from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

set_instance_params(client, "MP0", w="500n", l="30n", nf="4", m="2")
```

For analogLib devices, direct CDF access works:

```python
client.execute_skill(
    'cdfFindParamByName(cdfGetInstCDF('
    'car(setof(i geGetEditCellView()~>instances i~>name == "R0")))'
    ' "r")~>value = "1k"')
client.execute_skill('dbSave(geGetEditCellView())')
```

## Read Placement

```python
from virtuoso_bridge.virtuoso.schematic.reader import read_placement

p = read_placement(client, "myLib", "myCell")
for i in p["instances"]:
    print(i["name"], i["xy"], i["orient"])
```

## Tips

- Use `add_net_label_to_transistor` for MOS D/G/S/B — auto-detects stub direction
- Use `schematic_label_instance_term` / `schematic_create_wire_between_instance_terms` instead of guessing coordinates
- **Check & save before simulation**: `schCheck` + `dbSave` — otherwise netlisting fails with a blocking dialog
- **Schematic should be open in GUI** for Maestro to reference it correctly

## See also

- `references/schematic-python-api.md` — Python API reference
