"""Bounded reads of an explicit transient PSF, independent of a shared CIW."""

from __future__ import annotations

import math
import os
from pathlib import Path
from pathlib import PurePosixPath
import shlex
import subprocess
import tempfile
import time

from virtuoso_bridge.cadence_env import CadenceEnvironment
from virtuoso_bridge.virtuoso.ops import escape_skill_string


def _text(value, name):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be a nonempty string without control characters")
    return value


def _request(results_dir, signals, start, stop, step, max_samples):
    _text(results_dir, "results_dir")
    if not results_dir.startswith("/") or ".." in PurePosixPath(results_dir).parts:
        raise ValueError("results_dir must be an absolute POSIX path to one point's PSF")
    if isinstance(signals, str) or not signals:
        raise ValueError("signals must be a nonempty sequence of exact saved signal names")
    signals = list(signals)
    if not signals:
        raise ValueError("signals must not be empty")
    for signal in signals:
        _text(signal, "signal")
    if len(set(signals)) != len(signals):
        raise ValueError("signals must be unique")
    for name, value in (("start", start), ("stop", stop), ("step", step)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be finite seconds")
    if not 0 <= start < stop or step <= 0:
        raise ValueError("Require 0 <= start < stop and step > 0")
    if type(max_samples) is not int or not 1 <= max_samples <= 1_000_000:
        raise ValueError("max_samples must be an integer between 1 and 1000000")
    ratio = (stop - start) / step
    if not math.isfinite(ratio) or ratio >= max_samples:
        raise ValueError("Requested sampling grid exceeds max_samples")
    count = math.floor(ratio) + 1
    if count * len(signals) > max_samples:
        raise ValueError("Total samples across all signals exceed max_samples")
    # Never let floating-point rounding place the last sample past stop.
    while start + (count - 1) * step > stop:
        count -= 1
    return signals, count


def _script(results_dir, signals, start, stop, step, count):
    queries = " ".join(f'"{escape_skill_string(s)}"' for s in signals)
    escaped_dir = escape_skill_string(results_dir)
    return f'''procedure(vbWindowSignal(fp idx query)
 let((wave xx yy n first last at val)
  wave=getData(query ?result 'tran)
  unless(wave error("Signal unavailable"))
  xx=drGetWaveformXVec(wave) n=drVectorLength(xx)
  yy=drGetWaveformYVec(wave)
  unless(drVectorLength(yy)==n error("Mismatched waveform vectors"))
  unless(n>0 error("No committed samples"))
  first=drGetElem(xx 0) last=drGetElem(xx n-1)
  unless(numberp(first) && numberp(last) && first<=last error("Invalid time axis"))
  fprintf(fp "C\\t%d\\t%d\\t%.17g\\t%.17g\\n" idx n float(first) float(last))
  for(k 0 {count - 1}
   at=float({start:.17g})+k*float({step:.17g})
   when(at>=float({start:.17g}) && at<=float({stop:.17g}) && at>=first && at<=last
    val=value(wave at)
    unless(numberp(val) error("Not a real scalar waveform"))
    fprintf(fp "D\\t%d\\t%.17g\\t%.17g\\n" idx float(at) float(val))))
  t))
unless(errset(progn(
 fp=outfile("window.tsv" "w") unless(fp error("Cannot create output"))
 opened=errset(openResults("{escaped_dir}") t)
 idx=0
 foreach(query '({queries})
  if(opened && car(opened)
   then unless(errset(vbWindowSignal(fp idx query) t)
         fprintf(fp "E\\t%d\\tunreadable_signal\\n" idx))
   else fprintf(fp "E\\t%d\\tresults_unavailable\\n" idx))
  idx=idx+1)
 fprintf(fp "END\\n") close(fp)
) t) exit(1))
exit(0)
'''


def _parse(text, signals, start, stop, step, count):
    rows = text.splitlines()
    if not rows or rows[-1] != "END" or "END" in rows[:-1]:
        raise RuntimeError("Incomplete OCEAN export; no result is accepted")
    waves = {s: {"coverage": None, "time": [], "values": [], "error": None} for s in signals}
    for row in rows[:-1]:
        cols = row.split("\t")
        try:
            kind, index = cols[:2]
            idx = int(index)
            if not 0 <= idx < len(signals):
                raise ValueError("Invalid signal index")
            wave = waves[signals[idx]]
            if wave["error"]:
                raise ValueError("Data after signal error")
            if kind == "C" and len(cols) == 5 and wave["coverage"] is None:
                n, first, last = int(cols[2]), float(cols[3]), float(cols[4])
                if n <= 0 or not math.isfinite(first) or not math.isfinite(last) or first > last:
                    raise ValueError("Invalid coverage")
                wave["coverage"] = {"native_points": n, "first": first, "last": last}
            elif kind == "D" and len(cols) == 4 and wave["coverage"] is not None:
                at, val = float(cols[2]), float(cols[3])
                cov = wave["coverage"]
                if (not math.isfinite(at) or not math.isfinite(val)
                        or not max(start, cov["first"]) <= at <= min(stop, cov["last"])
                        or (wave["time"] and at <= wave["time"][-1]) or len(wave["time"]) >= count):
                    raise ValueError("Invalid sample")
                wave["time"].append(at)
                wave["values"].append(val)
            elif kind == "E" and len(cols) == 3 and cols[2] in ("unreadable_signal", "results_unavailable"):
                wave.update(time=[], values=[], error=cols[2])
            else:
                raise ValueError("Unexpected record")
        except (ValueError, IndexError) as exc:
            raise RuntimeError("Malformed OCEAN window export") from exc
    for wave in waves.values():
        cov = wave["coverage"]
        if not wave["error"]:
            if cov is None:
                raise RuntimeError("Missing signal record in OCEAN export")
            expected = [start + k * step for k in range(count)
                        if max(start, cov["first"]) <= start + k * step <= min(stop, cov["last"])]
            if len(expected) != len(wave["time"]) or any(
                    not math.isclose(a, b, rel_tol=1e-14, abs_tol=0) for a, b in zip(expected, wave["time"])):
                raise RuntimeError("Missing or off-grid samples in OCEAN export")
        wave["sample_count"] = len(wave["time"])
        wave["window_complete"] = bool(not wave["error"] and cov and wave["time"]
                                       and cov["first"] <= start and cov["last"] >= stop)
    complete = all(w["window_complete"] for w in waves.values())
    return {"state": "ok" if complete else "partial" if any(w["time"] for w in waves.values()) else "unavailable",
            "window_complete": complete, "simulation_complete": None, "signals": waves}


def read_transient_window(results_dir, signals, start, stop, step, *, runner=None,
                          cadence_env=None, ocean_bin="ocean", timeout=60, max_samples=100_000):
    """Read a finite, linearly interpolated transient window, without CIW calls.

    ``results_dir`` must identify exactly one PSF point (not a Maestro history
    or family). Signal names are exact ``getData`` keys. Times are seconds.
    Local execution requires POSIX; supply an SSHRunner to read on its host.
    That runner must reach the filesystem containing the PSF; loopback is never
    interpreted as permission to read the client's filesystem.

    A complete *window* does not establish simulation completion or acceptance.
    Growing PSF reads are per-signal observations, not an atomic snapshot. No
    extrapolation, simulator pause/flush, history update or automatic retry is
    performed. Native reading may load more data than the bounded output.

    Requires headless OCEAN and POSIX ``mktemp``/GNU ``timeout`` on the reader
    host. Only the isolated reader group is timed out, never a running simulator.
    On transport failure the owned scratch directory is retained for diagnosis.
    """
    signals, count = _request(results_dir, signals, start, stop, step, max_samples)
    _text(ocean_bin, "ocean_bin")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite positive seconds")
    if runner is None and os.name != "posix":
        raise RuntimeError("Local OCEAN requires POSIX; use an explicit SSH runner on Windows")
    env = cadence_env or CadenceEnvironment.from_env()
    code = _script(results_dir, signals, start, stop, step, count)
    # timeout bounds the isolated OCEAN process group even if the client disconnects.
    command = env.wrap("unset DISPLAY; exec timeout -k 5s " + shlex.quote(f"{timeout:g}s")
                       + " " + shlex.quote(ocean_bin) + " -nograph -restore read.ocn -log ocean.log < /dev/null")
    started = time.time()
    remote = None
    completed = False
    diagnostics = ""
    with tempfile.TemporaryDirectory(prefix="vb-waveform-") as tmp:
        root = Path(tmp)
        if runner is None:
            (root / "read.ocn").write_text(code, encoding="utf-8")
            proc = subprocess.run(command, shell=True, cwd=root, capture_output=True, text=True,
                                  timeout=timeout + 15)
            if proc.returncode:
                raise RuntimeError(f"OCEAN reader failed ({proc.returncode}): {proc.stderr[-2000:]}")
            diagnostics = (proc.stdout + "\n" + proc.stderr)[-4000:]
        else:
            made = runner.run_command("sh -c 'umask 077; mktemp -d /tmp/vb-waveform-XXXXXXXXXXXX'", timeout=20)
            remote = made.stdout.strip()
            if (made.returncode or not remote.startswith("/tmp/vb-waveform-")
                    or not remote[len("/tmp/vb-waveform-"):].isalnum()):
                raise RuntimeError("Could not allocate an isolated reader directory")
            try:
                up = runner.upload_text(code, remote + "/read.ocn", timeout=20)
                if up.returncode:
                    raise RuntimeError("Could not upload reader script")
                run = runner.run_command("sh -c " + shlex.quote("cd " + shlex.quote(remote) + " && " + command),
                                         timeout=timeout + 15)
                completed = True
                if run.returncode:
                    raise RuntimeError(f"OCEAN reader failed ({run.returncode}): {run.stderr[-2000:]}")
                diagnostics = (run.stdout + "\n" + run.stderr)[-4000:]
                got = runner.download(remote + "/window.tsv", root / "window.tsv", timeout=30)
                if got.returncode:
                    raise RuntimeError("Could not download reader output")
            except Exception as exc:
                raise RuntimeError(f"Transient read failed; scratch: {remote}; no simulation was restarted") from exc
        result = _parse((root / "window.tsv").read_text(encoding="utf-8"), signals, start, stop, step, count)
        if remote and completed:
            try:
                cleanup = runner.run_command("rm -rf -- " + shlex.quote(remote), timeout=20)
                if cleanup.returncode:
                    diagnostics += f"\nReader scratch cleanup failed: {remote}"
            except Exception:
                diagnostics += f"\nReader scratch cleanup unconfirmed: {remote}"
    result.update(results_dir=results_dir, requested_window={"start": start, "stop": stop, "step": step},
                  observed_at=started, finished_at=time.time(), atomic_snapshot=False, diagnostics=diagnostics)
    return result
