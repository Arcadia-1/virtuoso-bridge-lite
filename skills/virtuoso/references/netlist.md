# Netlist Reference

## Formats

### CDL (Circuit Description Language)

SPICE-compatible format, used for LVS verification and schematic import.

```
.SUBCKT cap_unit TOP BOT
C0 TOP BOT cap C=1e-14
.ENDS

.SUBCKT cap_array_4b TOP BOT<3:0>
XC_b0_0 TOP BOT<0> cap_unit
XC_b1_0 TOP BOT<1> cap_unit
.ENDS
```

Syntax:
- `.SUBCKT name pin1 pin2 ...` / `.ENDS`
- Instance: `name node1 node2 model [params]`
- Subcircuit instance: `Xname node1 node2 subcktName [params]`
- Bus notation: `BOT<3:0>`, `BOT<0>`

### Spectre

Cadence Spectre simulator format, used for simulation.

```
subckt cap_unit (TOP BOT)
    C0 (TOP BOT) capacitor c=1e-14
ends cap_unit

XC_b0_0 (TOP BOT\<0\>) cap_unit
```

Syntax:
- `subckt name (pin1 pin2)` / `ends name`
- Instance: `name (node1 node2) model param=value`
- Bus notation: `BOT\<0\>` (angle brackets escaped with backslash)
- Device names are full: `capacitor`, `resistor`, `inductor` (not `cap`, `res`, `ind`)

### Key Differences

| | CDL | Spectre |
|--|-----|---------|
| Purpose | LVS, schematic import | Simulation |
| Pin syntax | `TOP BOT` | `(TOP BOT)` |
| Device names | Short: `cap`, `res`, `ind` | Full: `capacitor`, `resistor`, `inductor` |
| Bus escaping | `BOT<0>` | `BOT\<0\>` |
| Subcircuit end | `.ENDS` | `ends name` |
| Parameters | `C=1e-14` | `c=1e-14` |
| Case | Mostly uppercase keywords | Lowercase |

## Parameter Name Mapping

Some parameters have different names in schematic CDF vs netlist:

| Schematic CDF | Spectre/CDL netlist | Description |
|---------------|---------------------|-------------|
| `acm` | `mag` | AC magnitude |
| `vdc` | `dc` | DC voltage |
| `r` | `r` | Resistance (same) |
| `c` | `c` | Capacitance (same) |

## Source Device Mapping

`analogLib/vsin` in the schematic becomes `vsource type=sine` in Spectre netlist. There is no separate `vsin` device in Spectre — it is a mode of `vsource`.

`spiceIn` importing a CDL with `vsin` will create `analogLib/vsource` (not `analogLib/vsin`). To get `vsin` in the schematic, either:
- Change the instance master manually after import
- Use `client.schematic.edit()` to add the source with the correct master
- Add the source via SKILL: `dbCreateInst(cv dbOpenCellView("analogLib" "vsin" "symbol") ...)`

## Import: CDL → Virtuoso Schematic

Use `spiceIn` (Cadence command-line tool). Must run via SSH, not via SKILL `system()`.

```bash
spiceIn -language SPICE \
  -netlistFile input.cdl \
  -outputLib PLAYGROUND_LLM \
  -reflibList "analogLib basic" \
  -devmapFile devmap.txt
```

Device mapping file (`devmap.txt`):
```
devselect := resistor res
devselect := capacitor cap
devselect := inductor ind
```

**Symbol generation** after spiceIn import:
```python
# IMPORTANT: must be a single-line SKILL string — multi-line f-strings
# with newlines cause SKILL parsing failure via bridge
client.execute_skill(f'schPinListToSymbol("{lib}" "{cell}" "symbol" schSchemToPinList("{lib}" "{cell}" "schematic"))')
```
The function works for all cells. Never manually create symbols. Verify with `ddGetObj(lib cell)~>views~>name`.

Key points:
- **Auto-wires** everything — instances, nets, pins all connected automatically
- **Auto-generates symbols** for subcircuits (if reference lib has them)
- **Must run via SSH** — `spiceIn` launches an internal Virtuoso process, calling it from SKILL `system()` will deadlock the CIW
- Cell names come from `.SUBCKT` names in the CDL
- `spiceIn` path: `{cdsGetInstPath()}/bin/spiceIn`
- Requires Cadence env: `LM_LICENSE_FILE`, `IC_HOME`, `LD_LIBRARY_PATH`

## Export: Virtuoso Schematic → Spectre Netlist

Use `maeCreateNetlistForCorner` (requires a temporary Maestro view).

```python
# Create temp maestro
ses = client.execute_skill(f'maeOpenSetup("{lib}" "{cell}" "maestro")').output.strip('"')
client.execute_skill(f'maeCreateTest("T1" ?lib "{lib}" ?cell "{cell}" ?view "schematic" ?simulator "spectre" ?session "{ses}")')
client.execute_skill(f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" ?view "maestro" ?session "{ses}")')

# Export
client.execute_skill(f'maeCreateNetlistForCorner("T1" "Nominal" "/tmp/netlist_dir")')

# Read: /tmp/netlist_dir/netlist/input.scs
client.download_file('/tmp/netlist_dir/netlist/input.scs', 'output/netlist.scs')
```

Key points:
- Outputs Spectre format (not CDL)
- Complete and correct — includes subcircuit hierarchy, model includes, simulator options
- `auCdl` export via `si -batch` does **not work** reliably outside Virtuoso (missing SKILL callbacks)

## Direct Schematic Read → Netlist

The schematic database (instances, nets, terminals) can be read directly via SKILL and assembled into any netlist format without relying on external netlisters. See `examples/01_virtuoso/schematic/02_read_connectivity.py`.

Key SKILL accessors:
- `cv~>instances` → all instances
- `inst~>instTerms` → instance terminals
- `instTerm~>net~>name` → connected net name
- `cv~>nets` → all nets
- `net~>instTerms` → all inst.term pairs on a net
- `cv~>terminals` → top-level pins and directions

This gives the same connectivity information as CDL or Spectre netlisters — just needs formatting into the target syntax. Pay attention to:
- Bus notation: `BOT<0>` in schematic vs `BOT\<0\>` in Spectre
- Device names: `cap` (CDL) vs `capacitor` (Spectre)
- Pin order: CDL uses positional, Spectre uses `(node1 node2)`
- Parameters: CDL `C=1e-14`, Spectre `c=1e-14`

## Roundtrip: Create → Export → Import

```
Python API → Virtuoso schematic
                ↓ maeCreateNetlistForCorner
           Spectre netlist
                ↓ text conversion (Spectre → CDL)
              CDL file
                ↓ spiceIn (SSH)
           Virtuoso schematic (new cell name)
```

Spectre → CDL conversion is simple text processing:
- `subckt name (pins)` → `.SUBCKT name pins`
- `ends name` → `.ENDS`
- `(node1 node2) capacitor c=` → `node1 node2 cap C=`
- Remove backslash escaping on bus brackets

## Sample Netlists

The same circuit in three representations: a 2-bit CDAC (cap_unit × [1,2]) with a 1Ω resistor from the capacitor top-plate to VOUT.

```
        VOUT ──[R0 1Ω]── TOP ──┬── cap_unit (BOT<0>)  ← bit0, ×1
                                ├── cap_unit (BOT<1>)  ← bit1, ×2
                                └── cap_unit (BOT<1>)
```

Sample files in `references/netlist_samples/` — a 2-stage RC low-pass cascade (rc_unit → rc_cascade_2 → tb_rc_cascade with vsin source). Generated from an actual CDL → spiceIn → Virtuoso → export flow, AC-verified (2-pole roll-off at 159 MHz).

- `netlist_samples/rc_cascade.cdl` — CDL input (source of truth, fed to spiceIn)
- `netlist_samples/rc_cascade.scs` — Spectre output (exported via `maeCreateNetlistForCorner`)
- `netlist_samples/rc_cascade.connectivity.txt` — schematic read (from `02_read_connectivity.py`, all 3 hierarchy levels)

## Examples

- `examples/01_virtuoso/schematic/02_read_connectivity.py` — read schematic connectivity via SKILL
- `examples/01_virtuoso/schematic/08_import_cdl_cap_array.py` — CDL → spiceIn import
- `examples/01_virtuoso/maestro/04_rc_filter_sweep.py` — includes Spectre netlist export via maeCreateNetlistForCorner
