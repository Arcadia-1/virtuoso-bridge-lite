# veriloga — import a `.va` file as a Cadence behavioural cellview

Single recipe: take a `.va` file living locally and turn it into a
Verilog-A cellview in a target OA library so it can be instantiated
in another schematic.

This is **not** a Verilog-A authoring tutorial — it covers only the
file/cellview interface (the part the bridge has to drive). The
contents of the `.va` are entirely up to you; `sample.va` here is a
trivial pass-through purely so the example is runnable.

## The 5-step path

The IC618-supported way is mechanical but multi-step. There is no
"create veriloga from .va" SKILL primitive, so we synthesise one:

1. **Placeholder schematic** with floating pins matching the .va's
   ports.  The symbol generator works off the schematic pin list, not
   off .va text — schematic must exist first.
2. **Symbol** generated from the schematic via `schSchemToPinList` +
   `schPinListToSymbol`.  Geometric pin sort ⇒ symbol layout follows
   schematic placement.
3. **Veriloga skeleton** generated from the symbol via
   `schViewToView ... "symbol" "veriloga" "schSymbolToPinList"
   "ahdlPinListToveriloga"`.  This drops a default
   `veriloga.va` into `<lib>/<cell>/veriloga/`.
4. **Overwrite the skeleton** with the user's `.va`, uploaded via
   `client.upload_file` to `<readPath>/<cell>/veriloga/veriloga.va`.
5. **Reparse** with `ahdlUpdateViewInfo` so Virtuoso replaces its
   cached ports with the real ones.

The script in `import_veriloga.py` runs this end-to-end.

## Usage

```bash
python import_veriloga.py <LIB> <CELL> <local-.va> \
    --inputs IN1 IN2 \
    --outputs OUT1 \
    --inout INOUT1
```

Pin direction comes from the CLI flags; the .va must agree
(Cadence cross-checks during reparse).  Use `--inout` for terminals
that should appear bidirectional on the symbol.

Quick smoke test with the bundled sample:

```bash
python import_veriloga.py PLAYGROUND_LLM sample sample.va --inout in out
```

## Why this is a recipe, not a `client.create_veriloga_cell()` helper

Same reasoning as `digital_import/`: step 3's `schViewToView` argument
keys (`"schSymbolToPinList"` / `"ahdlPinListToveriloga"`) are
Cadence-defined and have renamed across major IC releases.  Keeping
the recipe here as a copy-and-adjust example limits the blast radius
when an IC upgrade shifts the ground.

If you need something more elaborate (extra views, signal-direction
auto-detection from `.va`, bus-port handling), copy this script and
adapt — that's the point of the cookbook layout.
