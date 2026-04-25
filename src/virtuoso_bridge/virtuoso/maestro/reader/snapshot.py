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
from .session import (_fetch_window_state, natural_sort_histories,
                      sort_histories_by_mtime)


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


# Per-point artifacts pulled into ``snap_dir/<history>/``.
#
# Always captured (inputs + run logs):
#   netlist/*          everything the Virtuoso netlister generates —
#                      input.scs plus stimuli, include, modelpath,
#                      control, runObjFile, cdfInst*, etc.
#   psf/spectre.out    spectre stdout
#   psf/logFile        spectre logFile
#   <history>.log      OA history summary (maestro/results level)
#
# Captured when ``include_results=True`` (default; MB-scale per run):
#   psf/dcOp.dc           DC node voltages
#   psf/dcOpInfo.info     per-MOS operating point (gm, vth, vds, cgg ...)
#   psf/ac.ac             AC sweep result
#   psf/noise.noise       noise spectrum
#   psf/tran.tran         transient waveform
#   psf/pss.pss / psf/pnoise.pnoise / psf/pac.pac / psf/stb.stb / psf/xf.xf
#                         periodic / stability / transfer analyses
#   psf/variables_file    exact design-var values used
#   psf/spectre.dc|.ic|.fc  spectre state/initial-conditions aux
#   <history>.rdb         Maestro results DB (computed output expressions)
#   <history>.msg.db      Maestro run message DB
#
# Always skipped (PDK / netlister boilerplate, derivable elsewhere,
# or huge binary):
#   psf/modelParameter.info       — PDK model params (already in .scs models)
#   psf/primitives.info.primitives — PDK primitive catalog
#   psf/subckts.info.subckts       — PDK subckt dumps
#   psf/designParamVals.info       — derived param expansions
#   psf/element.info               — per-element (post-flatten) bias;
#                                    dcOpInfo.info covers design-level view
#   psf/outputParameter.info       — derived output-expression dumps
#   psf/*.raw                      — huge PSF binary waveforms
#   psf/wavedb/                    — proprietary waveform DB

# ---------------------------------------------------------------------------
# Per-point file whitelists — loaded from resources/snapshot_filter.yaml
# ---------------------------------------------------------------------------
# Edit ``snapshot_filter.yaml`` (section ``per_point.netlist`` /
# ``per_point.psf``) to control what ``snapshot -o`` pulls per point.
# The YAML is the source of truth; the tuples below are a minimal
# fallback used only if the file is unreadable (broken install etc.).

_DEFAULT_NETLIST_FILES: tuple[str, ...] = (
    "netlist", "input.scs", "qpInformation.ils", "paramInfo.ils",
)

_DEFAULT_PSF_FILES: tuple[str, ...] = (
    "spectre.out", "logFile",
    "dcOp.dc", "dcOpInfo.info", "variables_file",
    "*.ac", "*.dc", "*.tran", "*.noise",
    "*.pss", "*.pnoise", "*.pac", "*.pxf",
    "*.stb", "*.xf", "*.sens",
)


def _per_point_list(key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Return the ordered include-list from ``per_point.<key>`` in
    ``snapshot_filter.yaml``; fall back to *fallback* if the file or
    key is missing."""
    from ._parse_sdb import _load_filter_config
    cfg = _load_filter_config()
    raw = (cfg.get("per_point") or {}).get(key)
    if isinstance(raw, list) and raw:
        return tuple(str(x) for x in raw if x)
    return fallback


def _dump_run_artifacts(client: VirtuosoClient, snap_dir: Path, *,
                         history: str, lib_path: str, scratch_root: str,
                         lib: str, cell: str, view: str,
                         include_results: bool = True) -> None:
    """Pull per-point inputs (+ optionally results) for ``history`` into
    ``snap_dir/<history>/``.

    Single ssh round-trip: server-side ``find | tar`` packs all matched
    files into one tarball, one ``scp`` pulls it down, local extract
    rebuilds the per-point layout.

    ``include_results=True`` (default) also grabs the spectre simulation
    *result* files (dcOp/dcOpInfo/ac/noise/tran/etc.) and the
    Maestro-level ``.rdb`` / ``.msg.db`` DBs.  PDK / netlister info
    dumps (``modelParameter.info``, ``element.info``,
    ``primitives.info.primitives``, ...) and PSF binary waveforms
    (``*.raw``, ``wavedb/``) stay skipped — they duplicate the models
    or are proprietary binary blobs.  Set to ``False`` for an
    inputs-only snapshot.
    """
    if not (history and lib_path and scratch_root):
        return
    runner = client._tunnel._ssh_runner
    maestro_dir = f"{lib_path}/{cell}/{view}/results/maestro"
    log_remote = f"{maestro_dir}/{history}.log"
    hist_remote = (f"{scratch_root}/{lib}/{cell}/{view}"
                   f"/results/maestro/{history}")
    remote_tar = f"/tmp/vb_snap_{uuid.uuid4().hex}.tar"

    # Per-point find clauses — driven by resources/snapshot_filter.yaml.
    # Edit that YAML (per_point.netlist / per_point.psf) to change what's
    # pulled; this function rereads it on every call.  `-path '*/<dir>/*'`
    # scopes the match to the right subtree, each `-name` is exact or glob.
    netlist_files = _per_point_list("netlist", _DEFAULT_NETLIST_FILES)
    netlist_clause = " -o ".join(f"-name '{n}'" for n in netlist_files)
    clauses = f"\\( -path '*/netlist/*' \\( {netlist_clause} \\) \\)"
    if include_results:
        psf_files = _per_point_list("psf", _DEFAULT_PSF_FILES)
        psf_clause = " -o ".join(f"-name '{n}'" for n in psf_files)
        clauses += f" -o \\( -path '*/psf/*' \\( {psf_clause} \\) \\)"

    # Maestro-level extra files (siblings of <history>.log):
    #   always:            <history>.log
    #   include_results:   <history>.rdb, <history>.msg.db
    #
    # List both project and scratch_root locations; tar
    # --ignore-failed-read lets whichever path actually has the file
    # win.  Explorer-derived `.RO` runs only have these companion
    # files in scratch_root — without listing it here they'd be
    # silently missing from the snapshot.
    scratch_maestro_dir = f"{scratch_root}/{lib}/{cell}/{view}/results/maestro"
    maestro_extras = [
        log_remote,
        f"{scratch_maestro_dir}/{history}.log",
    ]
    if include_results:
        maestro_extras.extend([
            f"{maestro_dir}/{history}.rdb",
            f"{maestro_dir}/{history}.msg.db",
            f"{scratch_maestro_dir}/{history}.rdb",
            f"{scratch_maestro_dir}/{history}.msg.db",
        ])
    extras_str = " ".join(maestro_extras)

    # Include symlinks: Cadence's per-point netlist/ is largely symlinks
    # pointing into ``psf/.../netlist/`` (netlist itself, map, amap,
    # ihnl, designInfo, netlistHeader/Footer, ...).  ``-type f`` alone
    # would drop every one of them — including the main ``netlist``
    # file.  Match symlinks too, and pass ``-h`` to tar so the archive
    # stores the symlink target's contents under the symlink's name
    # (safer than preserving the link on the client, whose directory
    # layout wouldn't satisfy the relative target).
    tar_cmd = (
        f"find {hist_remote} \\( -type f -o -type l \\) \\( {clauses} \\) "
        f"-print 2>/dev/null "
        f"| tar -chf {remote_tar} -P -T - --ignore-failed-read "
        f"{extras_str} 2>/dev/null && echo OK"
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
                # GNU tar folds the second-and-onwards copies of the
                # same inode into *hard link* entries pointing at the
                # first copy (happens here because the per-point
                # netlist/ dir is almost entirely symlinks into a
                # shared psf/.../netlist/; with --dereference the
                # first path archived wins and every other path
                # becomes a hrw-... hard-link row).  tarfile.isfile()
                # returns False for those rows, so naive code silently
                # drops the main netlist file along with map / amap /
                # ihnl / designInfo / netlistHeader / netlistFooter /
                # statAlters.  Resolve linkname back to the anchor
                # member and copy its bytes out under *this* member's
                # path.
                if m.isfile():
                    payload = m
                elif m.islnk():
                    try:
                        payload = tf.getmember(m.linkname)
                    except KeyError:
                        continue
                    if not payload.isfile():
                        continue
                else:
                    continue
                # Map remote absolute path → local relative path under
                # snap_dir/<history>/.  Maestro-level ``<history>.log``
                # / ``.rdb`` / ``.msg.db`` land flat in hist_dir;
                # per-point files keep their relative path.
                base = m.name.rsplit("/", 1)[-1]
                if base in (f"{history}.log",
                            f"{history}.rdb",
                            f"{history}.msg.db"):
                    target = hist_dir / base
                elif f"/{history}/" in m.name:
                    target = hist_dir / m.name.split(f"/{history}/", 1)[1]
                else:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(payload)
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
             output_root: str | None = None,
             history: str | None = None) -> dict:
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

    *history*:  when given, the disk dump targets that specific history
    name (e.g. ``"Interactive.160"``) instead of the mtime / current-
    history auto-pick.  Useful for pulling older runs side-by-side.
    Only meaningful with ``output_root``.
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
        # If the caller pinned a specific history (--history CLI flag),
        # use it verbatim — skip the auto-pick.  Otherwise fall back to
        # the mtime-first resolution below.
        #
        # Auto-pick order (when history is None):
        # Prefer mtime, because the user's intuition of "newest" is
        # "what I last ran", not "what the GUI result panel happens to
        # have loaded".  axlGetCurrentHistory sticks to an earlier
        # Explorer run when the user has since launched an Interactive
        # sim without re-loading its results; mtime reflects the actual
        # latest run on disk.
        #
        # Fallbacks in order:
        #   1. Disk mtime — newest-modified history files win.
        #   2. Session's currently-loaded history (axlGetCurrentHistory~>name).
        #   3. Natural sort by name.
        latest_history = history or (
            (sort_histories_by_mtime(bundle.get("hist_files_mtime") or [])
             or [None])[0] or
            bundle.get("current_history") or
            (natural_sort_histories(bundle.get("hist_files") or [])
             or [""])[-1]
        )
        snap_dir = _dump_to_dir(
            client, bundle=bundle, lib=lib, cell=cell, view=view,
            sess=sess, latest_history=latest_history,
            output_root=output_root,
        )
        out["output_dir"] = str(snap_dir)
        out["latest_history"] = latest_history

    return out
