#!/usr/bin/env python3
"""Build a signoff website from captured PNGs and simulation results.

Stdlib only. Run: python3 tools/signoff.py [--out preview/signoff] [--title T]
An optional Anatomy figure is supplied as anatomy.html in the output directory.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # repo root (tools/signoff.py)
REPO = BASE
PREVIEW = REPO / "preview"
STATE = PREVIEW / "data" / "signoff_state.json"
NOTELOG = PREVIEW / "data" / "errors.jsonl"


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()[:12]


def shots() -> list[dict]:
    d = PREVIEW / "shots"
    out = []
    for p in sorted(d.glob("*.png")) + sorted(d.glob("*.svg")):
        out.append({"file": p.name, "bytes": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")})
    return out


def sim_results() -> list[dict]:
    rows = []
    wave = PREVIEW / "data" / "wave.json"
    if wave.exists():
        w = json.loads(wave.read_text())
        v = [x for x in w.get("vout", []) if isinstance(x, (int, float))]
        rows.append({"run": "psf_test.tran", "tool": "psf_read",
                     "detail": "n=%d min=%.4g max=%.4g mean=%.4g" % (
                         w.get("n", len(v)), min(v), max(v), sum(v) / len(v)) if v else "no numeric samples"})
    check = PREVIEW / "data" / "netlist_check.txt"
    if check.exists():
        lines = check.read_text().splitlines()
        rows.append({"run": "rc_cascade.scs", "tool": "netlist_check",
                     "detail": "%d findings" % len(lines)})
    tools = PREVIEW / "data" / "tools.json"
    if tools.exists():
        t = json.loads(tools.read_text())
        rows.append({"run": "bridge", "tool": "tools/list",
                     "detail": "%d tools live" % len(t.get("tools", []))})
    ring = PREVIEW / "data" / "sims.jsonl"
    if ring.exists():
        latest = {}
        for line in ring.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            key = (r.get("netlist"), r.get("output_dir"))
            latest[key] = r  # last write wins, so old runs drop out
        skip = {"clk", "clock", "time"}
        for r in latest.values():
            det = "%s %s, %d signals" % (r.get("mode", "?"), "ok" if r.get("ok") else "FAIL",
                                         r.get("signals", 0))
            osc = r.get("osc") or {}
            if osc:
                picks = [k for k in sorted(osc) if k.lower() not in skip] or sorted(osc)
                first = picks[0]
                det += ", %s %.3g Hz" % (first, osc[first].get("freq_hz", 0))
            rows.append({"run": Path(r.get("netlist", "?")).name, "tool": "spectre_run",
                         "detail": det})
    return rows


CSS = ("body{font-family:system-ui,Arial,sans-serif;max-width:1000px;margin:0 auto;"
       "padding:24px;background:#0d1117;color:#e6edf3}"
       "h1{font-size:24px}h2{font-size:18px;margin-top:32px;border-bottom:2px solid #2a3139;padding-bottom:4px}"
       "table{border-collapse:collapse;width:100%;font-size:14px;background:#161b22}"
       "td,th{border-bottom:1px solid #2a3139;padding:7px 10px;text-align:left;vertical-align:top}"
       "th{color:#9aa4b2;font-size:12px;text-transform:uppercase}"
       ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}"
       ".card{background:#161b22;border:1px solid #2a3139;border-radius:10px;padding:10px}"
       "img{width:100%;border-radius:8px;background:#fff}img.big{max-width:100%}"
       ".small{color:#9aa4b2;font-size:13px}"
       "iframe{width:100%;height:520px;border:1px solid #2a3139;border-radius:10px;background:#0b0b0b}"
       "pre{background:#161b22;border:1px solid #2a3139;border-radius:8px;padding:10px;overflow:auto;font-size:12.5px;white-space:pre-wrap}"
       ".ok{color:#3fb950;font-weight:700}.bad{color:#f85149;font-weight:700}"
       ".fade{animation:fade 0.8s ease both}@keyframes fade{from{opacity:0}to{opacity:1}}")


def notes() -> list[dict]:
    out = []
    if NOTELOG.exists():
        for line in NOTELOG.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"level": "info", "message": line})
    return out[-50:]


def wave_stats(wave: Path | None = None) -> list[dict]:
    w = wave or PREVIEW / "data" / "wave.json"
    if not w.exists():
        return []
    d = json.loads(w.read_text())
    rows = []
    keys = [k for k in d if k not in ("n", "t", "time", "TIME")]
    for k in keys[:20]:
        v = [x for x in d.get(k, []) if isinstance(x, (int, float)) and x == x]
        if not v:
            continue
        mean = sum(v) / len(v)
        mean_square = sum(x * x for x in v) / len(v)
        rows.append({"signal": k.upper(), "n": len(v), "min": min(v), "max": max(v),
                     "mean": mean, "rms": mean_square ** 0.5, "first": v[0], "last": v[-1]})
    return rows


def build(out: Path, title: str, only_shots: list[str] | None = None,
          wave: Path | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    sh = shots()
    if only_shots:
        want = {s.strip() for s in only_shots if s.strip()}
        sh = [s for s in sh if s["file"] in want]
    for s in sh:
        data = (PREVIEW / "shots" / s["file"]).read_bytes()
        (out / s["file"]).write_bytes(data)
    # Revision: hash inputs, diff vs previous signoff OF THIS DIR.
    # State is keyed by out dir so scoped pages don't churn each other.
    cur = {s["file"]: sha(PREVIEW / "shots" / s["file"]) for s in sh}
    prev = {}
    rev = 1
    dirs = {}
    if STATE.exists():
        try:
            raw = json.loads(STATE.read_text())
            dirs = raw.get("dirs", {}) if isinstance(raw, dict) else {}
            mine = dirs.get(str(out), {})
            rev = int(mine.get("rev", 0)) + 1
            prev = mine.get("hashes", {})
        except Exception:
            pass
    added = [f for f in cur if f not in prev]
    changed = [f for f in cur if f in prev and prev[f] != cur[f]]
    removed = [f for f in prev if f not in cur]
    dirs[str(out)] = {"rev": rev, "hashes": cur}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"dirs": dirs,
                                 "stamp": datetime.now(timezone.utc).isoformat()}, indent=1))
    rows = sim_results()
    stats = wave_stats(wave)
    log = notes()
    gal = "\n".join(
        '<div class="card"><b>%s</b><p class="small">%s · %d KB</p>'
        '<img loading="lazy" src="%s" alt="%s"></div>' % (
            html.escape(s["file"]), s["mtime"], s["bytes"] // 1024, s["file"], html.escape(s["file"]))
        for s in sh)
    have_chart = any(s["file"] == "wave_chart.png" for s in sh)
    chartsec = ('<img class="big fade" src="wave_chart.png" alt="waveform chart">'
                if have_chart else
                "<p class=small>No wave chart for this scope.</p>")
    tab = "\n".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        html.escape(r["run"]), html.escape(r["tool"]), html.escape(r["detail"])) for r in rows)
    st = "\n".join("<tr><td>%s</td><td>%d</td><td>%.5g</td><td>%.5g</td><td>%.5g</td><td>%.5g</td><td>%.5g</td><td>%.5g</td></tr>" % (
        html.escape(s["signal"]), s["n"], s["min"], s["max"], s["mean"], s["rms"], s["first"], s["last"]) for s in stats)
    revline = "rev %d · +%d ~%d -%d since last" % (rev, len(added), len(changed), len(removed))
    revdetail = "added: %s · changed: %s · removed: %s" % (
        ", ".join(added) or "none", ", ".join(changed) or "none", ", ".join(removed) or "none")
    if log:
        errsec = "<h2>Exact errors and notes (%d)</h2>" % len(log) + "".join(
            '<pre class="fade"><b>[%s]</b> %s</pre>' % (
                html.escape(str(n.get("level", "info"))),
                html.escape(str(n.get("message", ""))[:4000])) for n in log)
    else:
        errsec = "<h2>Exact errors and notes</h2><p class=small>None logged. Add notes to preview/data/errors.jsonl.</p>"
    check = PREVIEW / "data" / "netlist_check.txt"
    fulllog = ("<h2>Full netlist check log</h2><pre>%s</pre>" % html.escape(check.read_text()) if check.exists() else "")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Embed a circuit-specific figure only when the agent supplies one.
    anat = out / "anatomy.html"
    if anat.is_file():
        anatsec = ("<h2>Anatomy figure</h2>"
                   '<iframe title="circuit anatomy" src="anatomy.html"></iframe>')
    else:
        anatsec = ("<h2>Anatomy figure</h2>"
                   "<p class=small>No circuit figure supplied. Install the external "
                   "Anatomy skill, then add <code>anatomy.html</code> in this output directory.</p>")
    page = ("<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            "<title>%s</title><style>%s</style></head><body>"
            "<h1>%s</h1><p class=small>Preview generated %s · %s · <span class=ok>review required</span></p>"
            "<p class=small>%s</p>"
            "%s"
            "<h2>Simulation graphs</h2>"
            "%s"
            "<table><tr><th>Signal</th><th>n</th><th>min</th><th>max</th><th>mean</th><th>rms</th><th>first</th><th>last</th></tr>%s</table>"
            "<h2>Simulation results</h2><table><tr><th>Run</th><th>Tool</th><th>Detail</th></tr>%s</table>"
            "%s%s"
            "<h2>All PNGs (%d)</h2><div class=grid>%s</div>"
            "</body></html>") % (html.escape(title), CSS, html.escape(title), stamp, revline,
                                 revdetail, anatsec, chartsec, st, tab, errsec, fulllog, len(sh), gal)
    (out / "index.html").write_text(page)
    (out / "rev.json").write_text(json.dumps(
        {"rev": rev, "stamp": stamp, "added": added, "changed": changed,
         "removed": removed, "shots": len(sh), "results": len(rows)}, indent=1))
    return {"out": str(out), "rev": rev, "shots": len(sh), "results": len(rows),
            "added": added, "changed": changed, "removed": removed, "stamp": stamp}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="preview/signoff")
    ap.add_argument("--title", default="Virtuoso Bridge — Signoff")
    ap.add_argument("--shots", default="",
                    help="Comma-separated shot files to include; default all")
    ap.add_argument("--wave", default="",
                    help="Wave JSON for the stats table; default preview/data/wave.json")
    a = ap.parse_args()
    info = build(Path(a.out), a.title,
                 only_shots=[s for s in a.shots.split(",") if s.strip()] or None,
                 wave=Path(a.wave) if a.wave else None)
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
