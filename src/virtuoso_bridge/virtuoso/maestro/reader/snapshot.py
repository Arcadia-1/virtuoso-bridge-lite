"""Top-level aggregator: ``snapshot()``.

Two modes via ``output_root=``:

- ``None`` (default) → SKILL-only sparse dict (~150ms, 2 round-trips).
- path             → also writes the disk dump (raw + YAML-filtered
                     XMLs, raw SKILL section dump, newest run's
                     artifacts) and sets ``output_dir`` on the dict.

Three non-overlapping tracks on disk: ``state_from_skill.txt`` (raw
SKILL alists verbatim) / ``state_from_sdb.xml`` (YAML-filtered sdb) /
``state_from_active_state.xml`` (YAML-filtered active.state).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

from ._parse_sdb import _sdb_active_tests, filter_active_state_xml, filter_sdb_xml
from .bundle import brief_bundle, full_bundle
from .session import _fetch_window_state, natural_sort_histories


# ---------------------------------------------------------------------------
# Disk-dump primitives
# ---------------------------------------------------------------------------

def _scp(client: VirtuosoClient, remote: str, local: Path) -> bool:
    """scp ``remote`` → ``local``; swallow errors.  ``True`` on success."""
    if not remote:
        return False
    try:
        client.download_file(remote, str(local))
    except Exception:
        return False
    return local.exists()


def _filter_to(local_raw: Path, target: Path, filter_fn) -> None:
    """Read ``local_raw`` → ``filter_fn(xml)`` → ``target``.  No-op if
    raw missing or filter returns empty."""
    if not local_raw.exists():
        return
    try:
        filt = filter_fn(local_raw.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return
    if filt:
        target.write_text(filt, encoding="utf-8")


def _dump_setup_xmls(client: VirtuosoClient, snap_dir: Path,
                     lib_path: str, cell: str, view: str) -> None:
    """scp + filter ``maestro.sdb`` and ``active.state``.  The
    active.state filter reads sdb's ``<active><tests>`` to drop
    Cadence tombstones (removed-test state the GUI doesn't clean up)."""
    if not lib_path:
        return
    local_sdb = snap_dir / "maestro.sdb"
    valid_tests: set[str] = set()
    if _scp(client, f"{lib_path}/{cell}/{view}/{view}.sdb", local_sdb):
        _filter_to(local_sdb, snap_dir / "state_from_sdb.xml", filter_sdb_xml)
        try:
            valid_tests = _sdb_active_tests(
                local_sdb.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    local_state = snap_dir / "active.state"
    if _scp(client, f"{lib_path}/{cell}/{view}/active.state", local_state):
        _filter_to(local_state, snap_dir / "state_from_active_state.xml",
                   lambda x: filter_active_state_xml(
                       x, valid_test_names=valid_tests or None))


def format_skill_sections(sections: list[tuple[str, str]]) -> str:
    """Format ``raw_sections`` as ``[label] value`` lines.

    Single line per section — SKILL alists are typically single-line
    anyway, so the bracket-label and value share one line for compact
    display.  Used by both ``state_from_skill.txt`` and the CLI brief
    stdout output (single source of truth).  No alist→dict parsing.
    """
    if not sections:
        return ""
    return "\n\n".join(
        f"[{label}] {(raw or '').strip()}" for label, raw in sections
    ) + "\n"


def _dump_skill_text(snap_dir: Path, sections: list[tuple[str, str]]) -> None:
    """Write ``state_from_skill.txt`` from ``sections``."""
    text = format_skill_sections(sections)
    if text:
        (snap_dir / "state_from_skill.txt").write_text(text, encoding="utf-8")


# Per-point artifacts pulled into ``snap_dir/<history>/``.  Text only —
# PSF binary waveforms / wavedb are huge and proprietary so we skip them.
# Add to this tuple to capture more files; the tar packs everything in
# one ssh round-trip regardless of count.
_RUN_FILE_NAMES = ("input.scs", "spectre.out", "logFile")


def _dump_run_artifacts(client: VirtuosoClient, snap_dir: Path, *,
                         history: str, lib_path: str, scratch_root: str,
                         lib: str, cell: str, view: str) -> None:
    """Pull every per-point ``input.scs`` / ``spectre.out`` / ``logFile``
    plus the OA ``.log`` for ``history`` into ``snap_dir/<history>/``.

    Single ssh round-trip: server-side ``find | tar`` packs all matched
    files into one tarball, one ``scp`` pulls it down, local extract
    rebuilds the per-point layout.  N points × 3 files = 1 ssh + 1 scp
    (vs N×3 scp's previously).
    """
    if not (history and lib_path and scratch_root):
        return
    runner = client._tunnel._ssh_runner
    log_remote = f"{lib_path}/{cell}/{view}/results/maestro/{history}.log"
    hist_remote = (f"{scratch_root}/{lib}/{cell}/{view}"
                   f"/results/maestro/{history}")
    remote_tar = f"/tmp/vb_snap_{uuid.uuid4().hex}.tar"

    # find by exact name in the per-point subtree, then tar the matches
    # plus the OA log file in absolute-path mode (-P).  All in one ssh.
    name_clauses = " -o ".join(f'-name {n}' for n in _RUN_FILE_NAMES)
    tar_cmd = (
        f'find {hist_remote} -type f \\( {name_clauses} \\) -print 2>/dev/null '
        f'| tar -cf {remote_tar} -P -T - {log_remote} 2>/dev/null && echo OK'
    )
    r = runner.run_command(tar_cmd, timeout=30)
    if "OK" not in (r.stdout or ""):
        return

    local_tar = snap_dir / "vb_run.tar"
    try:
        if not _scp(client, remote_tar, local_tar):
            return
        import tarfile
        hist_dir = snap_dir / history
        hist_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(local_tar) as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                # Map remote absolute path → local relative path under
                # snap_dir/<history>/.  The OA .log is a sibling of the
                # history dir; per-point files keep their relative path.
                if m.name.endswith(f"{history}.log"):
                    target = hist_dir / f"{history}.log"
                elif f"/{history}/" in m.name:
                    target = hist_dir / m.name.split(f"/{history}/", 1)[1]
                else:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, open(target, "wb") as dst:
                    dst.write(src.read())
    finally:
        try:
            local_tar.unlink()
        except OSError:
            pass
        runner.run_command(f"rm -f {remote_tar}", timeout=10)


def _dump_to_dir(client: VirtuosoClient, *, bundle: dict, lib: str, cell: str,
                 view: str, sess: str, latest_history: str,
                 output_root: str) -> Path:
    """Orchestrate the 3 disk tracks → return the snapshot directory."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = Path(output_root) / f"{ts}__{lib}__{cell}"
    snap_dir.mkdir(parents=True, exist_ok=True)

    lib_path = bundle.get("lib_path") or ""
    _dump_setup_xmls(client, snap_dir, lib_path, cell, view)
    _dump_skill_text(snap_dir, bundle.get("raw_sections") or [])
    _dump_run_artifacts(
        client, snap_dir,
        history=latest_history, lib_path=lib_path,
        scratch_root=bundle.get("scratch_root") or "",
        lib=lib, cell=cell, view=view,
    )
    return snap_dir


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def snapshot(client: VirtuosoClient, *,
             output_root: str | None = None) -> dict:
    """Snapshot the focused maestro session.

    Returns a minimal dict.  ``raw_sections`` is the canonical setup
    view — list of ``(label, raw_skill_text)`` tuples, one per SKILL
    probe.  Everything else is window-state metadata or the disk-dump
    output dir.  No SKILL alist→Python parsing.

    Returned keys:

    * ``session`` — focused davSession id (``""`` if focus isn't a
      maestro window)
    * ``app`` / ``lib`` / ``cell`` / ``view`` / ``mode`` / ``unsaved`` —
      parsed from focused window title
    * ``raw_sections`` — list of ``(label, raw_text)`` tuples (the
      same content as ``state_from_skill.txt`` when ``output_root``
      is given)
    * ``output_dir`` — added when ``output_root`` is given

    With ``output_root="..."`` also writes the full disk dump to
    ``{output_root}/{YYYYMMDD_HHMMSS}__{lib}__{cell}/`` (raw + filtered
    XMLs, ``state_from_skill.txt``, newest-run artifacts).
    """
    win  = _fetch_window_state(client)
    sess = win["session"]
    lib, cell = win["lib"], win["cell"]
    view = win["view"] or "maestro"

    # Brief mode (no output_root) → 4 probes, 1 round-trip.
    # Disk-dump mode → full 16+ probes, 2 round-trips, plus path /
    # history info needed by _dump_to_dir.
    if not sess:
        bundle = {}
    elif output_root is None:
        bundle = brief_bundle(client, sess=sess, lib=lib, cell=cell, view=view)
    else:
        bundle = full_bundle(client, sess=sess, lib=lib, cell=cell, view=view)

    out: dict = {
        "session":      sess,
        "app":          win["application"],
        "lib":          lib, "cell": cell, "view": view,
        "mode":         win["mode"],
        "unsaved":      win["unsaved"],
        "raw_sections": bundle.get("raw_sections") or [],
    }

    if output_root is not None:
        if not sess:
            raise RuntimeError("No focused maestro window.")
        latest_history = (natural_sort_histories(bundle.get("hist_files") or [])
                          or [""])[-1]
        snap_dir = _dump_to_dir(
            client, bundle=bundle, lib=lib, cell=cell, view=view,
            sess=sess, latest_history=latest_history,
            output_root=output_root,
        )
        out["output_dir"] = str(snap_dir)

    return out
