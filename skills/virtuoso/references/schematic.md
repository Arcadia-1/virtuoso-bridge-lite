# Schematic Reference

## Edit Pattern

```python
with client.schematic.edit(lib, cell) as sch:
    sch.add_instance("analogLib", "vdc", (0, 0), "V0", params={"vdc": "0.9"})
    sch.add_instance("analogLib", "gnd", (0, -0.5), "GND0")
    sch.add_wire([(0, 0), (0, 0.5)])
    sch.add_pin("VDD", "inputOutput", (0, 1.0))
    sch.add_label("VDD", (0, 1.0))
    sch.add_net_label_to_instance_term("V0", "PLUS", "VDD")
    sch.add_wire_between_instance_terms("V0", "MINUS", "GND0", "gnd!")
```

## Read / Query

```python
client.schematic.open(lib, cell)
client.schematic.check(lib, cell)
client.schematic.save(lib, cell)
```

## Tips

- Use terminal-aware helpers (`add_net_label_to_instance_term`, `add_wire_between_instance_terms`) instead of guessing pin coordinates
- Use `add_pin_to_instance_term` to connect a top-level pin directly to an instance terminal
