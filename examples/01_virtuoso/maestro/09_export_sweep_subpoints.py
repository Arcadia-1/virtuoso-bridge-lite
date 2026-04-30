#!/usr/bin/env python3
"""Export waveforms from a parametric sweep, per sub-point.

Background — why this isn't ``read_results`` / ``export_waveform``:

For a Maestro test with a parametric sweep (e.g. ``set_var("dec_val",
"0:15")``), each completed run produces a *family* of result
directories under
``<sim_root>/<cell>/maestro/results/maestro/Interactive.<N>/<P>/``
(one ``<P>`` sub-directory per sweep point).  History names look like
``Interactive.3/4`` to identify "run 3, sweep point 4".

Calling ``maeOpenResults(?history "Interactive.3/4")`` consistently
fails with ``(ASSEMBLER-2233) Could not Load Results`` — the maestro
results loader does not accept the sub-point notation.  ``read_results``
returns nothing useful for these histories either.

The workaround that *does* work: bypass Maestro entirely and drive
**OCEAN** directly with the absolute path to the sub-point's PSF
directory.  OCEAN is the lower-level Cadence simulation result API and
has no such limitation.  We do this once per sweep point.

This recipe assumes you already know the sweep dimensions and the
absolute results root on the remote (which you can get from
``snapshot()``'s on-disk dump).  Use ``snapshot(client, output_root=...)``
upstream to discover them if you haven't recorded them.

Usage::

    python 09_export_sweep_subpoints.py \\
        --psf-root /home/.../<cell>/maestro/results/maestro/Interactive.3 \\
        --signals  /CLK_OUT /DOUT \\
        --num-points 16 \\
        --analysis  tran \\
        --remote-tmp /tmp/wf-export \\
        --local-out  ./waves
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from virtuoso_bridge import VirtuosoClient


def export_one_point(
    client: VirtuosoClient,
    psf_dir: str,
    signal: str,
    analysis: str,
    remote_out: str,
    *,
    open_timeout: int = 30,
    print_timeout: int = 30,
) -> None:
    """Pull one signal from one sweep point into ``remote_out`` (.txt)."""
    # OCEAN openResults takes the on-disk path to the PSF directory.
    # selectResult picks the analysis (tran / ac / dc / noise / ...).
    # ocnPrint dumps numeric (time, value) pairs into a flat text file.
    #
    # We use three separate execute_skill calls so each can have its
    # own per-call timeout — and so a slow openResults on one point
    # doesn't poison the next point's export.  All three are part of
    # the OCEAN session that lives in the Virtuoso process; state
    # persists across calls.
    client.execute_skill(f'openResults("{psf_dir}")', timeout=open_timeout)
    client.execute_skill(f"selectResult('{analysis})", timeout=open_timeout)
    client.execute_skill(
        f'ocnPrint(v("{signal}") ?output "{remote_out}")',
        timeout=print_timeout,
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--psf-root", required=True,
        help="absolute remote path to the run dir (Interactive.<N>/) "
             "containing per-sweep-point sub-directories",
    )
    p.add_argument(
        "--signals", nargs="+", required=True,
        help="SKILL signal expressions, e.g. /CLK_OUT or v('/X /Y')",
    )
    p.add_argument(
        "--num-points", type=int, required=True,
        help="number of sweep points (sub-directories named 1..N)",
    )
    p.add_argument(
        "--analysis", default="tran",
        help="analysis type (tran/ac/dc/noise/...). default: tran",
    )
    p.add_argument(
        "--remote-tmp", default="/tmp/vb_sweep_export",
        help="remote scratch dir for ocnPrint outputs",
    )
    p.add_argument(
        "--local-out", type=Path, default=Path("./waves"),
        help="local dir to download exports into",
    )
    args = p.parse_args()

    client = VirtuosoClient.from_env()
    args.local_out.mkdir(parents=True, exist_ok=True)
    client.execute_skill(f'system("mkdir -p {args.remote_tmp}")', timeout=10)

    for pt in range(1, args.num_points + 1):
        psf_dir = f"{args.psf_root}/{pt}/{args.analysis}-{args.analysis}.tran.tran"
        # Note: actual PSF subdir name varies by Cadence version /
        # analysis (e.g. ``tran-tran.tran.tran`` vs ``psf``).  Print
        # ``ls`` on the remote if unsure.

        for sig in args.signals:
            sig_safe = re.sub(r"[^A-Za-z0-9_]+", "_", sig).strip("_") or "sig"
            remote_out = f"{args.remote_tmp}/pt{pt}_{sig_safe}.txt"
            print(f"[pt {pt:>3}/{args.num_points}] {sig} → {remote_out}")
            try:
                export_one_point(client, psf_dir, sig, args.analysis, remote_out)
            except Exception as e:
                print(f"        FAILED: {e}")
                continue
            local_out = args.local_out / f"pt{pt}_{sig_safe}.txt"
            client.download_file(remote_out, local_out)

    print(f"\nDone. Files in {args.local_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
