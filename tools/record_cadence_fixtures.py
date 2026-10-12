#!/usr/bin/env python3
"""Record the Cadence fixtures replayed by tests/test_recorded_cadence_regressions.py.

Needs only a plain Virtuoso session reached through the bridge (no PDK) and a
writable scratch library. Run it against a dedicated session, never a CIW
someone is using: it briefly shows a blocking form that closes itself.

    python tools/record_cadence_fixtures.py --lib scratch_lib [--env lab.env]

Recorded data is scrubbed (host name, user and paths) before it is written to
tests/fixtures/cadence/. Commit the result together with manifest.json.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import threading
import time
import uuid
from datetime import date
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cadence"
FORM_TITLE = "VB fixture tall form"
# The property list x11_dismiss_dialog.py requests during inspection.
XPROP_FIELDS = (
    "_NET_WM_PID WM_TRANSIENT_FOR WM_CLIENT_LEADER WM_CLIENT_MACHINE _NET_WM_STATE "
    "_NET_WM_NAME WM_NAME WM_CLASS WM_STATE _NET_WM_WINDOW_TYPE"
)

# A blocking form about 700 px tall, like the PDK information forms some
# foundry kits display from libInit. A timer cancels it so the CIW recovers.
TALL_FORM_SKILL = (
    "let((field form) "
    "field = hiCreateMLTextField(?name 'vbFixtureText ?value \"tall blocking form fixture\" "
    "?editable nil) "
    "form = hiCreateAppForm(?name 'vbFixtureTallForm "
    f"?formTitle \"{FORM_TITLE}\" "
    "?fields list(list(field 10:10 640:640 0)) ?initialSize list(670 708) "
    "?buttonLayout 'OKCancel) "
    "hiRegTimer(\"hiFormCancel(vbFixtureTallForm)\" {tenths}) "
    "hiDisplayForm(form) t)"
)

CAPTURE_SCRIPT = """\
pid={pid}
for kv in $(tr '\\0' '\\n' < /proc/$pid/environ | grep -E '^(DISPLAY|XAUTHORITY)='); do export "$kv"; done
# Window titles contain non-ASCII text; a non-UTF-8 locale mangles them.
export LC_ALL=en_US.UTF-8
tree=$(xwininfo -root -tree)
form=$(for id in $(printf '%s\\n' "$tree" | grep -F '"{title}"' | awk '{{print $1}}'); do
  xwininfo -id "$id" | grep -q 'IsViewable' && echo "$id" && break; done)
ciw=$(printf '%s\\n' "$tree" | grep -F ' - Log: ' | head -1 | awk '{{print $1}}')
[ -n "$form" ] && [ -n "$ciw" ] || {{ echo "window not found" >&2; exit 3; }}
echo "@@FORM_ID $form"; echo "@@FORM_XPROP"; xprop -id "$form" {fields}
echo "@@FORM_XWININFO"; xwininfo -id "$form"
echo "@@CIW_ID $ciw"; echo "@@CIW_XPROP"; xprop -id "$ciw" {fields}
echo "@@CIW_XWININFO"; xwininfo -id "$ciw"
echo "@@HOST $(hostname)"; echo "@@USER $(id -un)"
"""


def _sections(text: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    key = None
    for line in text.splitlines():
        if line.startswith("@@"):
            name, _, rest = line[2:].partition(" ")
            key = name
            out[key] = [rest] if rest else []
        elif key:
            out[key].append(line)
    return {k: "\n".join(v).strip() + "\n" for k, v in out.items()}


def _geometry(xwininfo: str) -> dict[str, int]:
    fields = {"x": "Absolute upper-left X", "y": "Absolute upper-left Y", "w": "Width", "h": "Height"}
    return {key: int(re.search(rf"{label}:\s+(-?\d+)", xwininfo).group(1)) for key, label in fields.items()}


def _scrub(text: str, host: str, user: str) -> str:
    text = text.replace(f'"{host}"', '"eda-host"')
    text = re.sub(r"Log: [^\"\s]+", "Log: /home/designer/work/CDS.log", text)
    return text.replace(user, "designer") if user else text


def record_tall_form(client) -> dict:
    from virtuoso_bridge import VirtuosoClient

    pid = client.get_virtuoso_pid()
    guarded = VirtuosoClient.from_env(timeout=60)
    guarded.dialogs.enable_guard(pid=pid, protect_inflight=True, timeout=15)
    result = {}

    def show():
        result["r"] = guarded.execute_skill(TALL_FORM_SKILL.format(tenths=200), timeout=8)

    thread = threading.Thread(target=show)
    thread.start()
    time.sleep(5)
    script = CAPTURE_SCRIPT.format(pid=pid, title=FORM_TITLE, fields=XPROP_FIELDS)
    captured = client.ssh_runner.run_command("sh -c " + shlex.quote(script), timeout=30)
    thread.join()
    handle = result["r"].metadata.get("request_handle")
    deadline = time.monotonic() + 60
    while handle and time.monotonic() < deadline:
        if guarded.requests.receipt(handle, timeout=10).metadata.get("request_state") == "completed":
            break
        time.sleep(2)
    if captured.returncode != 0:
        raise RuntimeError(f"capture failed: {captured.stderr.strip()}")
    parts = _sections(captured.stdout)
    host, user = parts["HOST"].strip(), parts["USER"].strip()
    return {
        "tall_form.xprop": _scrub(parts["FORM_XPROP"], host, user),
        "ciw.xprop": _scrub(parts["CIW_XPROP"], host, user),
        "windows.json": json.dumps({
            "form": {"id": parts["FORM_ID"].strip(), "geometry": _geometry(parts["FORM_XWININFO"])},
            "ciw": {"id": parts["CIW_ID"].strip(), "geometry": _geometry(parts["CIW_XWININFO"])},
            "virtuoso_pid": pid,
        }, indent=2) + "\n",
    }


# Requests that fail in SKILL. The multi-line one goes through the daemon's
# load()-and-re-raise path, the others through the single-line path.
SKILL_ERRORS = (
    'error("VB fixture error %d" 3)',
    "car(1)",
    "vbFixtureUndefinedFunction()",
    "progn(\n  car(1)\n)",
)


def record_skill_errors(client) -> dict:
    replies = []
    for code in SKILL_ERRORS:
        result = client.execute_skill(code)
        reply = result.metadata.get("skill_error") or (result.errors or [""])[0]
        replies.append({"skill": code, "reply": reply})
    return {"skill_errors.json": json.dumps(replies, indent=2) + "\n"}


def record_schcheck_warnings(client, lib: str) -> dict:
    from virtuoso_bridge.virtuoso.schematic.ops import schematic_create_inst_by_master_name as inst
    from virtuoso_bridge.virtuoso.schematic.ops import schematic_label_instance_term as label

    cell = f"VB_FIXTURE_floating_{uuid.uuid4().hex[:8]}"
    try:
        with client.schematic.create(lib, cell) as sch:
            sch.add(inst("analogLib", "res", "symbol", "R0", 0.0, 0.0, "R0"))
            sch.add(label("R0", "PLUS", "A"))
            sch.add(label("R0", "MINUS", "B"))
        report = client.schematic.check_and_save(lib, cell)
        lines = [line.replace(cell, "floating_res") for line in report.check_log_lines]
        return {"schcheck_floating.lines": "\n".join(lines) + "\n",
                "schcheck_floating.json": json.dumps({
                    "check_error_count": report.check_error_count,
                    "check_warning_count": report.check_warning_count,
                }, indent=2) + "\n"}
    finally:
        client.execute_skill(
            f'progn(foreach(cv dbGetOpenCellViews() when(cv~>cellName == "{cell}" dbPurge(cv))) '
            f'ddDeleteObj(ddGetObj("{lib}" "{cell}")))')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lib", required=True, help="writable scratch library visible in the CIW")
    parser.add_argument("--env", help="bridge .env file to use")
    parser.add_argument("--out", type=Path, default=FIXTURES)
    args = parser.parse_args()
    if args.env:
        from virtuoso_bridge.env import set_runtime_env_file

        set_runtime_env_file(str(Path(args.env).expanduser()))
    from virtuoso_bridge import VirtuosoClient

    client = VirtuosoClient.from_env(timeout=60)
    files = {}
    files.update(record_tall_form(client))
    files.update(record_schcheck_warnings(client, args.lib))
    files.update(record_skill_errors(client))
    version = client.execute_skill("getVersion(t)").output.strip().strip('"').strip()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (args.out / name).write_text(content, encoding="utf-8")
    manifest_path = args.out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest.update({
        "virtuoso": version,
        "recorded": date.today().isoformat(),
        "recorder": "tools/record_cadence_fixtures.py",
        "pdk": None,
        "files": sorted(set(manifest.get("files", [])) | set(files)),
    })
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(files)} fixtures to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
