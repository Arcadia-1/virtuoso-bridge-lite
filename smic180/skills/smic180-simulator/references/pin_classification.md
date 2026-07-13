# Pin Classification Rules - SMIC180 SIM-IO Testbench

The LLM reads `pin_info.json` (pin names, directions, schematic coordinates)
alongside this document and produces `pin_classifications.json`.
The code in `sim_io/flow.py` consumes that JSON to build the testbench schematic.

---

## 0. IO Ring Device -> Domain Mapping

IO ring pins come from pad devices defined in
`SMIC180-ioring/references/enrichment_rules_SMIC180.md` and
`SMIC180-ioring/io_ring/schematic/devices/IO_device_info_SMIC180.json`.

| SMIC180 Pad Devices | Domain | Typical signal names |
|---|---|---|
| `PVDD1ANA` | `analog` | AVDD-prefixed analog supply |
| `PVSS1ANA` | `analog` | AVSS-prefixed analog ground |
| `PVDD1CDG` | `digital` | Consumer VDD (e.g., VIOLD) |
| `PVSS1CDG` | `digital` | Consumer VSS (e.g., GIOLD) |
| `PVDD2CDG` | `digital` | Provider VDDPST (e.g., VIOHD) |
| `PVSS2CDG` | `digital` | Provider VSSPST (e.g., GIOHD) |
| `PDDW0412SCDG` | `digital` | Functional IO (RST, D*, SCK, SDI, SDO, EN, PG, etc.) |

The supply pin naming in a specific design may differ from these examples -
always infer the domain from the actual pad device type in the enrichment rules,
or from name patterns and surrounding context in `pin_info.json`.

---

## 1. Three-Step Classification Process

### Step 1 - Device Class Assignment

Assign one `device_class` to every **outer (left-side)** pin.
Do **not** classify `_CORE` pins - the code handles them automatically.

| `device_class` | How to identify | Domain |
|---|---|---|
| `analog_power` | `PVDD1ANA` device; analog supply names (AVDD*) | `analog` |
| `analog_ground` | `PVSS1ANA` device; analog ground names (AVSS*, AVS*) | `analog` |
| `analog_input` | Differential pair inputs, bias inputs for analog circuits | `analog` |
| `analog_output` | Amplifier/LDO output pins | `analog` |
| `dig_consumer_power` | `PVDD1CDG` device; consumer VDD names (VIOLD, VDDIB) | `digital` |
| `dig_consumer_ground` | `PVSS1CDG` device; consumer VSS names (GIOLD, GNDIB) | `digital` |
| `dig_provider_power` | `PVDD2CDG` device; provider VDDPST names (VIOHD, VIOHA) | `digital` |
| `dig_provider_ground` | `PVSS2CDG` device; provider VSSPST names (GIOHD, GIOHA) | `digital` |
| `digital_io_input` | `PDDW0412SCDG`; direction=input (EN, RST, SCK, SDI) | `digital` |
| `digital_io_output` | `PDDW0412SCDG`; direction=output (PG, SDO, D*) | `digital` |

**Classification priority** (first match wins):
1. `PVDD2CDG` / `PVSS2CDG` by exact name -> `dig_provider_power` / `dig_provider_ground`
2. `PVDD1CDG` / `PVSS1CDG` by exact name -> `dig_consumer_power` / `dig_consumer_ground`
3. `PVDD1ANA` / `PVSS1ANA` by exact name -> `analog_power` / `analog_ground`
4. Digital IO direction/name patterns -> `digital_io_input` or `digital_io_output`
5. Ambiguous digital bidirectional -> default to `digital_io_input` (conservative)

### Step 2 - Inner Pin Resolution (code-handled, LLM awareness only)

After symbol redistribution, each outer (left) pin has a corresponding inner (right) pin:
- If `{pin_name}_CORE` exists in `pin_info.json` -> inner = `{pin_name}_CORE`
- Otherwise -> inner = `{pin_name}` duplicate on the right side

The code resolves this automatically. The LLM **does not** need to specify inner pin names.
However, the LLM must know this to correctly determine the `local_pvss` inner pin reference.

### Step 3 - Analog Local Ground Zone Assignment (**LLM JUDGMENT REQUIRED**)

Each `analog_ground` (PVSS) device defines a **local ground zone**.
Every `analog_power` pin must be assigned to exactly one zone.

**Grouping rules (apply in this order):**

1. **Name suffix matching (highest confidence)** - strip the type prefix (`AVDD/AVSS`)
   and compare the remaining suffix to the PVSS pin name.
2. **y-coordinate proximity (for ambiguous pins)** - assign to the `analog_ground` pin
   with the closest `y` value.

Output: declare `analog_local_grounds[]` at the top level of the JSON.
Each `analog_power` pin must have `local_pvss` set to the name of its zone's PVSS device.

---

## 2. Topology Rules per Device Class

### 2.1 `analog_ground` (PVSS1ANA)

Only the **outer** side gets a `vdc~=0` source. The inner PVSS pin is NOT driven by a
separate device - it is the common MINUS node shared by the inner `idc` (from `analog_power`).

```json
{
  "name": "AVSS",
  "device_class": "analog_ground",
  "domain": "analog",
  "local_pvss": "AVSS",
  "confidence": 0.95,
  "reason": "PVSS1ANA device -> analog_ground"
}
```

### 2.2 `analog_power` (PVDD1ANA)

```json
{
  "name": "AVDD",
  "device_class": "analog_power",
  "domain": "analog",
  "local_pvss": "AVSS",
  "stimulus": "vdc",
  "stimulus_params": {"vdc": "0.87"},
  "inner_stimulus": "idc",
  "inner_params": {"idc": "2.7m"},
  "confidence": 0.95,
  "reason": "PVDD1ANA device -> analog_power; local ground AVSS"
}
```

### 2.3 `analog_input`

Used for amplifier differential pair inputs, LNA inputs, etc.

**Differential pair rules:**
- Positive (VIP/INP): DC bias only, no AC stimulus
- Negative (VIN/INN): DC bias + AC stimulus
- Both inputs must have the same DC bias voltage (typically VDD/2)

**The vsource AC parameter name is `acm` (not `mag`):**

```json
{
  "name": "VIN",
  "device_class": "analog_input",
  "domain": "analog",
  "stimulus": "vdc",
  "stimulus_params": {"vdc": "0.9", "acm": "1"},
  "confidence": 0.90,
  "reason": "PMOS diff pair negative input; AC stimulus for gain measurement"
}
```

```json
{
  "name": "VIP",
  "device_class": "analog_input",
  "domain": "analog",
  "stimulus": "vdc",
  "stimulus_params": {"vdc": "0.9"},
  "confidence": 0.90,
  "reason": "PMOS diff pair positive input; DC reference only"
}
```

### 2.4 `analog_output`

```json
{
  "name": "OUT",
  "device_class": "analog_output",
  "domain": "analog",
  "load": "cap",
  "load_params": {"c": "2p"},
  "confidence": 0.90,
  "reason": "OTA output pin; capacitive load for stability test"
}
```

### 2.5 `dig_consumer_power` (PVDD1CDG)

```json
{
  "name": "VIOLD",
  "device_class": "dig_consumer_power",
  "domain": "digital",
  "stimulus": "vdc",
  "stimulus_params": {"vdc": "0.87"},
  "inner_stimulus": "idc",
  "inner_params": {"idc": "5.3m"},
  "confidence": 0.90,
  "reason": "PVDD1CDG consumer -> dig_consumer_power"
}
```

### 2.6 `dig_consumer_ground` (PVSS1CDG)

```json
{
  "name": "GIOLD",
  "device_class": "dig_consumer_ground",
  "domain": "digital",
  "confidence": 0.90,
  "reason": "PVSS1CDG consumer -> dig_consumer_ground"
}
```

### 2.7 `dig_provider_power` (PVDD2CDG)

```json
{
  "name": "VIOHD",
  "device_class": "dig_provider_power",
  "domain": "digital",
  "stimulus": "vdc",
  "stimulus_params": {"vdc": "1.72"},
  "confidence": 0.90,
  "reason": "PVDD2CDG provider -> dig_provider_power"
}
```

### 2.8 `dig_provider_ground` (PVSS2CDG)

```json
{
  "name": "GIOHD",
  "device_class": "dig_provider_ground",
  "domain": "digital",
  "confidence": 0.90,
  "reason": "PVSS2CDG provider -> dig_provider_ground"
}
```

### 2.9 `digital_io_input` (PDDW0412SCDG, direction=input)

```json
{
  "name": "EN",
  "device_class": "digital_io_input",
  "domain": "digital",
  "stimulus": "vpulse",
  "stimulus_params": {"v1": "0", "v2": "1.72", "per": "7n", "tr": "0.1n", "tf": "0.1n", "pw": "3.5n"},
  "confidence": 0.90,
  "reason": "PDDW0412SCDG direction=input -> digital_io_input"
}
```

### 2.10 `digital_io_output` (PDDW0412SCDG, direction=output)

```json
{
  "name": "PG",
  "device_class": "digital_io_output",
  "domain": "digital",
  "load": "cap",
  "load_params": {"c": "10p"},
  "confidence": 0.90,
  "reason": "PDDW0412SCDG direction=output -> digital_io_output"
}
```

---

## 3. Top-Level Result Structure

```json
{
  "lib": "LLM_Layout_Design",
  "cell": "IO_RING_ldo",
  "vdd_value": 0.9,
  "vio_low": 0.9,
  "vio_high": 1.8,
  "analog_local_grounds": [
    {"pvss_name": "AVSS", "members": ["AVDD"]}
  ],
  "digital_low_gnd": "GIOLD",
  "digital_supply_pairs": [
    {"power": "VIOLD", "ground": "GIOLD", "idc": "5.3m"}
  ],
  "pins": [...]
}
```

---

## 4. LLM Self-Check Before Writing Output

- [ ] Every `analog_power` pin has `local_pvss` set
- [ ] Every `analog_ground` pin has `local_pvss` = its own name
- [ ] `analog_local_grounds[].members` covers ALL `analog_power` pins
- [ ] `digital_low_gnd` is set to the consumer ground pin name (e.g., GIOLD)
- [ ] `digital_supply_pairs` covers consumer power/ground pairs
- [ ] All `stimulus_params` use non-round values (no clean integers/halves)
- [ ] All digital IO pins classified as input or output (no bidirectional)
- [ ] Only left-side pins (no `_CORE` suffix, side="left") appear in `pins[]`
- [ ] Confidence < 0.7 -> flag reason for review
- [ ] **Analog domain pins do NOT have `inner_stimulus`** (only digital IO pads use inner current)
- [ ] **AC stimulus uses `acm` parameter name, NOT `mag`**
- [ ] **Differential input pair: both inputs have same DC bias voltage**
- [ ] **Only one input of a diff pair has AC stimulus (`acm: "1"`)`

---

## 5. vsource CDF Parameter Reference

The following table lists the correct CDF parameter names for `analogLib/vsource`.
Agent must use these exact names in `stimulus_params`.

| CDF Parameter | Type | Description | Example |
|---|---|---|---|
| `dc` | float | DC offset voltage (V) | `"1.8"` |
| `acm` | float | AC magnitude (linear) | `"1"` |
| `acp` | float | AC phase (degrees) | `"0"` |
| `type` | string | Source type | `"dc"` |

> **Common mistake**: Using `mag` instead of `acm` for AC magnitude.
> The `mag` parameter does not exist on `analogLib/vsource`.

---

## 6. Analog Circuit Classification Examples (OpAmp, Bandgap, LDO)

When classifying pins for **analog-only** circuits (not IO rings), use these device classes.

### analog_input

Differential or single-ended analog input pins. These receive a DC bias voltage
plus optional AC small-signal stimulus for AC analysis.

`json
{
  "name": "VIP",
  "device_class": "analog_input",
  "domain": "analog",
  "stimulus": "vdc",
  "stimulus_params": {"dc": "0.9", "acm": "1", "acp": "0"},
  "confidence": 0.95,
  "reason": "OpAmp non-inverting input -> analog_input"
}
`

For **differential pairs**, set AC on ONE input only:
`json
{
  "name": "VIP",
  "device_class": "analog_input",
  "domain": "analog",
  "stimulus": "vdc",
  "stimulus_params": {"dc": "0.9", "acm": "1", "acp": "0"},
  ...
},
{
  "name": "VIN",
  "device_class": "analog_input",
  "domain": "analog",
  "stimulus": "vdc",
  "stimulus_params": {"dc": "0.9"},
  ...
}
`

### analog_output

Analog output pins (amplifier output, LDO output, etc.). Only get a capacitive load.

`json
{
  "name": "VOUT",
  "device_class": "analog_output",
  "domain": "analog",
  "load": "cap",
  "load_params": {"c": "1p"},
  "confidence": 0.95,
  "reason": "OpAmp output -> analog_output"
}
`

### analog_power (for OpAmp supply)

`json
{
  "name": "VDD",
  "device_class": "analog_power",
  "domain": "analog",
  "local_pvss": "VSS",
  "confidence": 0.95,
  "reason": "OpAmp positive supply -> analog_power"
}
`

### analog_ground (for OpAmp ground)

`json
{
  "name": "VSS",
  "device_class": "analog_ground",
  "domain": "analog",
  "local_pvss": "VSS",
  "confidence": 0.95,
  "reason": "OpAmp ground -> analog_ground"
}
`

### bias_current (for OpAmp bias pins)

`json
{
  "name": "IBIAS",
  "device_class": "bias_current",
  "domain": "analog",
  "stimulus": "isource",
  "stimulus_params": {"dc": "-10u"},
  "confidence": 0.95,
  "reason": "OpAmp bias current pin -> bias_current (use isource, NOT vsource)"
}
`

> **Critical**: Bias current pins MUST use isource (current source), NOT source.
> The netlist will have isource in the deck; source creates a voltage source
> which is wrong for current-biased circuits.

### OpAmp Testbench Classification Example

For a 3-pin OpAmp with VIP, VIN, VOUT, VDD, VSS, IBIAS:

`json
{
  "lib": "MyLib",
  "cell": "AMP_Bandgap",
  "vdd_value": 1.8,
  "analog_local_grounds": [
    {"pvss_name": "VSS", "members": ["VDD"]}
  ],
  "pins": [
    {"name": "VIP", "device_class": "analog_input", "domain": "analog",
     "stimulus": "vdc", "stimulus_params": {"dc": "0.9", "acm": "1", "acp": "0"}},
    {"name": "VIN", "device_class": "analog_input", "domain": "analog",
     "stimulus": "vdc", "stimulus_params": {"dc": "0.9"}},
    {"name": "VOUT", "device_class": "analog_output", "domain": "analog",
     "load": "cap", "load_params": {"c": "1p"}},
    {"name": "VDD", "device_class": "analog_power", "domain": "analog",
     "local_pvss": "VSS"},
    {"name": "VSS", "device_class": "analog_ground", "domain": "analog",
     "local_pvss": "VSS"},
    {"name": "IBIAS", "device_class": "bias_current", "domain": "analog",
     "stimulus": "isource", "stimulus_params": {"dc": "-10u"}}
  ]
}
`

### AC Analysis for Analog Circuits

When the circuit requires AC analysis (gain, bandwidth, phase margin), the
sim_config.json should include:

`json
{
  "analyses": [
    {"name": "dc", "enabled": true},
    {"name": "ac", "enabled": true,
     "sweep": {"param": "freq", "start": "1", "stop": "10G", "dec": "100"}},
    {"name": "tran", "enabled": true, "stop": "100n", "errpreset": "conservative"}
  ]
}
`

> **Note**: AC analysis requires at least one source with cm > 0.
> Only ONE input of a differential pair should have AC stimulus.