# Reading a Running Transient

`read_transient_window` reads already-flushed transient samples in an independent
headless OCEAN process. It works with a standalone Spectre raw directory or one
explicit Maestro test/corner/point PSF directory. No Virtuoso client or daemon is
needed, and it does not change the GUI's selected history or results context.

```python
from virtuoso_bridge.spectre import read_transient_window

# Run locally on the POSIX host where OCEAN and these results are available.
window = read_transient_window(
    "/simulation/myTB/maestro/results/maestro/Interactive.7/1/TRAN/psf",
    ["OUT", r"DOUT\<0\>"],
    start=0.0, stop=20e-9, step=10e-12,
)
print(window["state"], window["signals"]["OUT"]["coverage"])
```

For remote results, pass an existing `SSHRunner` as `runner=`. Use the runner for
the host whose filesystem contains the PSF, not necessarily the CIW/daemon host.
The function neither opens a Bridge nor closes the caller-owned runner. Windows
clients must use this remote mode; local execution requires POSIX.

```python
from virtuoso_bridge.cadence_env import CadenceEnvironment
from virtuoso_bridge.transport.ssh import SSHRunner

runner = SSHRunner(host="compute-host", user="designer")
try:
    window = read_transient_window(
        "/simulation/point/psf", ["OUT"], 0.0, 20e-9, 10e-12,
        runner=runner, cadence_env=CadenceEnvironment.from_env(), timeout=60,
    )
finally:
    runner.close()
```

## Selection and Limits

- Supply the absolute PSF directory for **one point**, not the Maestro history
  root. This deliberately avoids guessing the latest history, test or corner.
- Pass exact saved `getData` keys, including raw bus escapes. There is no slash
  insertion, hierarchy mapping, expression evaluation or fallback signal alias.
- Times are finite seconds. The grid is `start + k * step` up to `stop`; a
  non-divisible stop is not appended. Values are interpolated by OCEAN only
  inside each signal's observed coverage. The grid is never re-anchored.
- `max_samples` defaults to 100,000 **total values across all signals**, not
  native PSF points. It caps output, not the native reader's memory or disk I/O.
  Select a few signals and a modest interval before reading large PSFXL files.
- The reader host needs OCEAN, POSIX `mktemp` and GNU `timeout`. `ocean_bin=`
  selects one executable, not a shell command. The existing Cadence environment
  selector is reused. The reader has its own working directory and no DISPLAY.
- An unreadable index or missing/family/non-real waveform is reported as
  unavailable for that signal; valid sibling signals remain available. Native
  messages are retained in bounded `diagnostics`. No index rebuild is attempted.

## Return Contract

`signals[name]` contains `coverage` (`native_points`, `first`, `last`), `time`,
`values`, `sample_count`, `window_complete` and `error`. Coverage and times use
seconds. `error` is `None`, `unreadable_signal`, or `results_unavailable`; the
last two do **not** prove that the signal was never saved. It may not yet be
flushed or its result index may be unreadable. A failed signal has no samples.

Top-level `state` is `ok` when all requested windows are covered, `partial` when
some samples can be read, or `unavailable` when none can be read. A complete
window requires coverage through the requested stop, even if that stop is not
on the sampling grid. `simulation_complete` is always `None`: neither a complete
window nor an existing PSF file establishes simulator completion or signoff.

The result records the source path, requested window and observation start/end
timestamps. `atomic_snapshot=False` is intentional: growing results are read
sequentially per signal and are not a coherent multi-signal filesystem snapshot.
The caller must decide whether the available interval is adequate for analysis.

Execution/transport failures and malformed/truncated output raise rather than
return plausible empty results. A remote failure reports the isolated scratch
path for diagnosis; do not interpret it as a failed simulator or restart a run.
The timeout targets only the owned reader group. The API never pauses/flushes a
simulation, saves a history, repairs PSF metadata or retries the simulation.
