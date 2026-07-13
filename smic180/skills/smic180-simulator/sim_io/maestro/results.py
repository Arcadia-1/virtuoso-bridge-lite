"""Parse MaestroSimResult outputs into Python-accessible measurements dict.

Converts the Maestro per-point output table (from read_results()) into a
per-pin measurements dict.  For pass/fail verification, read maestro_detail.csv
which contains Maestro's own spec evaluation results.

Output name -> pin metric mapping (set up by _auto_generate_outputs in setup.py):
  vmax_<pin>  -> pins[pin]["vmax"]   (ymax of transient voltage)
  vmin_<pin>  -> pins[pin]["vmin"]   (ymin of transient voltage)
  I_<pin>     -> pins[pin]["iavg"]   (branch current, taken as abs)
  P_<pin>     -> pins[pin]["pavg"]   (v * i power expression, taken as abs)
"""

from __future__ import annotations

from sim_io.maestro.run import MaestroSimResult
from sim_io.pin_types import PinInfo, classify_pin_heuristic


def parse_maestro_measurements(
    mae_result: MaestroSimResult,
    pins: list[PinInfo],
    *,
    classifications: dict | None = None,
    vdd: float = 1.8,
) -> dict:
    """Convert MaestroSimResult.points -> measurements.json-compatible dict.

    Parameters
    ----------
    mae_result     : result from run_maestro_sim()
    pins           : pin list from DutContext (PinInfo objects)
    classifications: LLM pin_classifications dict {pin_name: PinClassification}
    vdd            : supply voltage (for context, not used in mapping)

    Returns a dict with structure::

        {
          "status": "ok",
          "analysis": "tran",
          "source": "maestro",
          "num_pins_measured": <int>,
          "num_pins_total": <int>,
          "pins": {
            "<pin_name>": {
              "pad_type": "power" | "digital_input" | ...,
              "vmax": <float>,      # present for digital/clock/reset pins
              "vmin": <float>,
              "vpp":  <float>,
              "iavg": <float>,      # present for power pins (abs)
              "pavg": <float>,      # present for power pins (abs)
            },
            ...
          }
        }
    """
    if not mae_result.sim_ok or not mae_result.points:
        return {
            "status": "error",
            "errors": ["Maestro simulation failed or produced no result points"],
        }

    # Flatten all per-point scalar outputs into {output_name: float}.
    # Single Run mode has exactly one point; sweeps have more - take first.
    #
    # Maestro CSV column mapping is inconsistent between versions and
    # single-run vs sweep modes.  Sometimes the "Nominal" column contains
    # the spec expression (e.g. "> 0.9*1.8") and the "Spec" column holds
    # the actual measurement (e.g. "1.7").  We try both fields and pick
    # whichever parses as a float.
    outputs: dict[str, float] = {}
    for pt in mae_result.points:
        for out_name, info in (pt.get("outputs") or {}).items():
            if not isinstance(info, dict):
                continue
            # Try "value" then "spec" - whichever yields a float first
            for field in ("value", "spec"):
                val_str = (info.get(field) or "").strip()
                if not val_str or val_str.lower() in ("nil", "n/a", "---", ""):
                    continue
                try:
                    outputs[out_name] = float(val_str)
                    break
                except (ValueError, TypeError):
                    continue
        # NOTE: For sweeps, all points are processed.
        # For Single Run, there is exactly one point.
        # Previously only the first point was used (break here).
        # Now all points are flattened for sweep support.

    pin_measurements: dict[str, dict] = {}
    for pin in pins:
        pad_type = (
            classifications[pin.name].pin_type
            if classifications and pin.name in classifications
            else classify_pin_heuristic(pin)
        )
        if pad_type == "ground":
            continue

        m: dict = {"pad_type": pad_type}

        vmax = outputs.get(f"vmax_{pin.name}")
        vmin = outputs.get(f"vmin_{pin.name}")
        if vmax is not None:
            m["vmax"] = vmax
        if vmin is not None:
            m["vmin"] = vmin
        if vmax is not None and vmin is not None:
            m["vpp"] = vmax - vmin

        iavg = outputs.get(f"I_{pin.name}")
        pavg = outputs.get(f"P_{pin.name}")
        if iavg is not None:
            m["iavg"] = abs(iavg)
        if pavg is not None:
            m["pavg"] = abs(pavg)

        if not (set(m.keys()) - {"pad_type"}):
            m["error"] = "no scalar outputs in Maestro results for this pin"

        pin_measurements[pin.name] = m

    n_ok = sum(1 for v in pin_measurements.values() if "error" not in v)
    return {
        "status": "ok",
        "analysis": "tran",
        "source": "maestro",
        "num_pins_measured": n_ok,
        "num_pins_total": len(pins),
        "pins": pin_measurements,
    }


def parse_all_points(
    mae_result,
    pins,
    *,
    classifications=None,
    vdd=1.8,
) -> list[dict]:
    """Parse ALL points from Maestro results (for sweeps/corners).

    Returns a list of measurement dicts, one per point.
    Each dict has the same structure as parse_maestro_measurements().
    """
    from sim_io.pin_types import classify_pin_heuristic

    if not mae_result.sim_ok or not mae_result.points:
        return []

    all_point_results = []
    for pt in mae_result.points:
        outputs = {}
        for out_name, info in (pt.get("outputs") or {}).items():
            if not isinstance(info, dict):
                continue
            for field in ("value", "spec"):
                val_str = (info.get(field) or "").strip()
                if not val_str or val_str.lower() in ("nil", "n/a", "---", ""):
                    continue
                try:
                    outputs[out_name] = float(val_str)
                    break
                except (ValueError, TypeError):
                    continue

        pin_measurements = {}
        for pin in pins:
            pad_type = (
                classifications[pin.name].pin_type
                if classifications and pin.name in classifications
                else classify_pin_heuristic(pin)
            )
            if pad_type == "ground":
                continue

            m = {"pad_type": pad_type}
            vmax = outputs.get(f"vmax_{pin.name}")
            vmin = outputs.get(f"vmin_{pin.name}")
            if vmax is not None:
                m["vmax"] = vmax
            if vmin is not None:
                m["vmin"] = vmin
            if vmax is not None and vmin is not None:
                m["vpp"] = vmax - vmin

            iavg = outputs.get(f"I_{pin.name}")
            pavg = outputs.get(f"P_{pin.name}")
            if iavg is not None:
                m["iavg"] = abs(iavg)
            if pavg is not None:
                m["pavg"] = abs(pavg)

            pin_measurements[pin.name] = m

        all_point_results.append({
            "point": pt.get("point", 0),
            "parameters": pt.get("parameters", {}),
            "pins": pin_measurements,
        })

    return all_point_results
