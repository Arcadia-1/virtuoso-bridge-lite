"""Helpers for exporting schematic netlists through Virtuoso's netlister."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import parse_sexpr

_SKILL_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SchematicNetlistExportResult(TypedDict):
    """Result metadata from a schematic netlist package export."""

    source_file: str
    source_dir: str
    output_dir: str
    input_file: str
    skill_result: Any
    download_result: Any


def _skill_bool(value: bool) -> str:
    return "t" if value else "nil"


def _skill_symbol(value: str, *, name: str) -> str:
    if not _SKILL_SYMBOL_RE.fullmatch(value):
        raise ValueError(f"{name} must be a simple SKILL symbol name")
    return f"'{value}"


def _result_output(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("output", ""))
    return str(getattr(result, "output", "") or "")


def _result_ok(result: Any) -> bool:
    if isinstance(result, dict):
        status = result.get("status")
        return status in ("success", ExecutionStatus.SUCCESS)
    return bool(getattr(result, "ok", False))


def _result_errors(result: Any) -> list[str]:
    if isinstance(result, dict):
        errors = result.get("errors", [])
        return [str(error) for error in errors]
    return [str(error) for error in getattr(result, "errors", [])]


def _set_result_output(result: Any, output: str) -> Any:
    if isinstance(result, dict):
        updated = dict(result)
        updated["output"] = output
        return updated
    if hasattr(result, "model_copy"):
        return result.model_copy(update={"output": output})
    try:
        result.output = output
    except Exception:
        pass
    return result


def _require_result_ok(result: Any, default_error: str) -> None:
    if _result_ok(result):
        return
    errors = "; ".join(_result_errors(result)) or default_error
    raise RuntimeError(errors)


def _decode_skill_string(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return bytes(text[1:-1], "utf-8").decode("unicode_escape")
    return text


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _replace_path_preserving_existing(source: Path, destination: Path) -> None:
    """Install ``source`` at ``destination`` while keeping old data recoverable."""
    if not _path_exists(destination):
        source.rename(destination)
        return

    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    destination.rename(backup)
    try:
        source.rename(destination)
    except Exception:
        if not _path_exists(destination) and _path_exists(backup):
            backup.rename(destination)
        raise
    else:
        if _path_exists(backup):
            _remove_path(backup)


def schematic_export_netlist_skill(
    lib: str,
    cell: str,
    *,
    view: str = "schematic",
    simulator: str = "spectre",
    recreate_all: bool = True,
) -> str:
    """Build SKILL to create a schematic netlist and return ``input.scs``.

    The generated SKILL uses the OCEAN netlisting flow:
    ``simulator`` → ``design`` → ``createNetlist``. Virtuoso returns the
    generated simulator input file. The Python wrapper downloads the
    containing netlist directory so adjacent includes such as ``ade_e.scs``
    stay with ``input.scs``.
    """
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_view = escape_skill_string(view)
    simulator_symbol = _skill_symbol(simulator, name="simulator")
    recreate = _skill_bool(recreate_all)
    return (
        "let((vbSimResult vbDesignResult vbNetlistResult vbSourceFile) "
        "unless(and(isCallable('simulator) isCallable('design) "
        "isCallable('createNetlist) isCallable('simplifyFilename)) "
        'error("netlist API unavailable")) '
        f"vbSimResult = errset(simulator({simulator_symbol}) nil) "
        'unless(vbSimResult && car(vbSimResult) error("simulator failed")) '
        f'vbDesignResult = errset(design("{escaped_lib}" "{escaped_cell}" "{escaped_view}" "r") nil) '
        'unless(vbDesignResult && car(vbDesignResult) error("design failed")) '
        "when(isCallable('ddsRefresh) errset(ddsRefresh() nil)) "
        f"vbNetlistResult = errset(createNetlist(?recreateAll {recreate} ?display nil) nil) "
        "vbSourceFile = if(vbNetlistResult then car(vbNetlistResult) else nil) "
        'unless(vbSourceFile error("createNetlist failed")) '
        "vbSourceFile = simplifyFilename(vbSourceFile) "
        "vbSourceFile)"
    )


def export_schematic_netlist(
    client: Any,
    lib: str,
    cell: str,
    output_dir: str | Path,
    *,
    view: str = "schematic",
    simulator: str = "spectre",
    recreate_all: bool = True,
    timeout: int = 120,
) -> SchematicNetlistExportResult:
    """Export a schematic netlist package to ``output_dir``.

    ``createNetlist`` produces an ``input.scs`` file plus adjacent support
    files. Downloading the containing directory preserves relative includes.
    Existing ``output_dir`` contents are replaced only after the new package
    has downloaded and the expected input file is present.

    Returns a dictionary with:
    ``source_file`` (Virtuoso host ``input.scs``), ``source_dir`` (Virtuoso
    host netlist package directory), ``output_dir`` (local package directory),
    ``input_file`` (downloaded local simulator input), ``skill_result``, and
    ``download_result``.
    """
    skill = schematic_export_netlist_skill(
        lib,
        cell,
        view=view,
        simulator=simulator,
        recreate_all=recreate_all,
    )
    skill_result = client.execute_skill(skill, timeout=timeout)
    if not _result_ok(skill_result):
        errors = "; ".join(_result_errors(skill_result)) or "createNetlist failed"
        raise RuntimeError(errors)

    source_file = _decode_skill_string(_result_output(skill_result))
    if not source_file or source_file == "nil":
        raise RuntimeError("createNetlist did not return a netlist path")

    source_path = PurePosixPath(source_file)
    if not source_path.is_absolute():
        raise RuntimeError(f"createNetlist returned relative netlist path: {source_file}")
    source_dir = source_path.parent.as_posix()
    destination = Path(output_dir)
    tmp_destination = destination.with_name(
        f".{destination.name}.tmp-{uuid.uuid4().hex}"
    )
    try:
        download_result = client.download_file(
            source_dir,
            tmp_destination,
            timeout=timeout,
            recursive=True,
        )
        if not _result_ok(download_result):
            errors = "; ".join(_result_errors(download_result)) or "netlist download failed"
            raise RuntimeError(errors)

        tmp_input_file = tmp_destination / "input.scs"
        if not tmp_destination.is_dir() or not tmp_input_file.is_file():
            raise RuntimeError(f"downloaded netlist is missing input.scs: {tmp_input_file}")

        _replace_path_preserving_existing(tmp_destination, destination)
        download_result = _set_result_output(download_result, str(destination))
    except Exception:
        if tmp_destination.exists():
            if tmp_destination.is_dir():
                shutil.rmtree(tmp_destination)
            else:
                tmp_destination.unlink()
        raise

    return {
        "source_file": source_file,
        "source_dir": source_dir,
        "output_dir": str(destination),
        "input_file": str(destination / "input.scs"),
        "skill_result": skill_result,
        "download_result": download_result,
    }


def schematic_import_netlist_skill(
    lib: str,
    cell: str,
    *,
    netlist_view: str = "netlist",
    schematic_view: str = "schematic",
    overwrite: bool = False,
    param_file: str | Path = "",
    spicein_log_file: str | Path = "",
) -> str:
    """Build SKILL to convert an imported netlist view into a schematic view.

    External tools such as ``spiceIn`` are intentionally not invoked here:
    they can start their own Virtuoso process and should be run from the
    Python/shell side.  This SKILL only touches the live DFII database.
    """
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_netlist_view = escape_skill_string(netlist_view)
    escaped_schematic_view = escape_skill_string(schematic_view)
    escaped_param_file = escape_skill_string(str(param_file))
    escaped_spicein_log = escape_skill_string(str(spicein_log_file))
    temp_schematic_view = f"__vb_import_{uuid.uuid4().hex}" if overwrite else schematic_view
    escaped_temp_schematic_view = escape_skill_string(temp_schematic_view)

    if not overwrite:
        return (
            "let((vbSchematicObj vbNetlistObj vbConnOk vbDestViewName) "
            f'when("{escaped_netlist_view}" == "{escaped_schematic_view}" '
            'error("netlist and schematic views must differ")) '
            f'vbSchematicObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_schematic_view}") '
            "when(vbSchematicObj "
            'ddReleaseObj(vbSchematicObj) error("target schematic exists")) '
            f'vbDestViewName = "{escaped_temp_schematic_view}" '
            f'vbNetlistObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_netlist_view}") '
            'unless(vbNetlistObj error("source netlist view not found")) '
            "unless(isCallable('conn2Sch) error(\"conn2Sch API unavailable\")) "
            f'vbConnOk = errset(conn2Sch("{escaped_lib}" "{escaped_cell}" "{escaped_netlist_view}" '
            f'?destLibName "{escaped_lib}" ?destCellName "{escaped_cell}" '
            '?destViewName vbDestViewName ?block t) nil) '
            'unless(vbConnOk && car(vbConnOk) error("conn2Sch failed")) '
            f'list("imported" "{escaped_lib}" "{escaped_cell}" "{escaped_param_file}" '
            f'"{escaped_spicein_log}"))'
        )

    return (
        "let((vbSchematicObj vbNetlistObj vbTempObj vbTempCv vbConnOk vbCopyOk vbDestViewName) "
        f'when("{escaped_netlist_view}" == "{escaped_schematic_view}" '
        'error("netlist and schematic views must differ")) '
        f'vbSchematicObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_schematic_view}") '
        "when(vbSchematicObj "
        "ddReleaseObj(vbSchematicObj)) "
        f'vbDestViewName = "{escaped_temp_schematic_view}" '
        f'vbTempObj = ddGetObj("{escaped_lib}" "{escaped_cell}" vbDestViewName) '
        'when(vbTempObj unless(ddDeleteObj(vbTempObj) error("temporary schematic delete failed"))) '
        f'vbNetlistObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_netlist_view}") '
        'unless(vbNetlistObj error("source netlist view not found")) '
        "unless(isCallable('conn2Sch) error(\"conn2Sch API unavailable\")) "
        "unwindProtect("
        "progn("
        f'vbConnOk = errset(conn2Sch("{escaped_lib}" "{escaped_cell}" "{escaped_netlist_view}" '
        f'?destLibName "{escaped_lib}" ?destCellName "{escaped_cell}" '
        '?destViewName vbDestViewName ?block t) nil) '
        'unless(vbConnOk && car(vbConnOk) error("conn2Sch failed")) '
        "unless(isCallable('dbCopyCellView) error(\"dbCopyCellView API unavailable\")) "
        f'vbTempCv = dbOpenCellViewByType("{escaped_lib}" "{escaped_cell}" vbDestViewName "schematic" "r") '
        'unless(vbTempCv error("temporary schematic open failed")) '
        f'vbCopyOk = dbCopyCellView(vbTempCv "{escaped_lib}" "{escaped_cell}" "{escaped_schematic_view}" nil nil t) '
        'unless(vbCopyOk error("target schematic copy failed")) '
        ") "
        "progn("
        "when(vbTempCv when(isCallable('dbClose) dbClose(vbTempCv)) vbTempCv = nil) "
        f'vbTempObj = ddGetObj("{escaped_lib}" "{escaped_cell}" vbDestViewName) '
        "when(vbTempObj errset(ddDeleteObj(vbTempObj) nil)))) "
        f'list("imported" "{escaped_lib}" "{escaped_cell}" "{escaped_param_file}" '
        f'"{escaped_spicein_log}"))'
    )


def import_netlist_schematic(
    client: Any,
    lib: str,
    cell: str,
    netlist_file: str | Path,
    *,
    language: str = "Spectre",
    sim_name: str = "spectre",
    output_sim_name: str = "spectre",
    ref_libs: list[str] | tuple[str, ...] = ("analogLib", "basic"),
    netlist_view: str = "netlist",
    schematic_view: str = "schematic",
    overwrite: bool = False,
    dev_map_file: str | Path | None = None,
    run_dir: str | Path | None = None,
    timeout: int = 300,
) -> Any:
    """Import a netlist into a schematic view through shell ``spiceIn`` + SKILL.

    ``spiceIn`` is run outside the CIW SKILL channel, matching the project
    guidance for Cadence tools that may launch their own Virtuoso process.
    The live SKILL bridge is used for environment discovery, preflight checks,
    and the final ``conn2Sch`` database conversion.
    """
    context = _netlist_import_context(client, timeout=timeout)
    resolved_run_dir = _resolve_import_run_dir(run_dir, lib, cell)
    preflight = client.execute_skill(
        _schematic_import_preflight_skill(
            lib,
            cell,
            netlist_view=netlist_view,
            schematic_view=schematic_view,
            overwrite=overwrite,
        ),
        timeout=timeout,
    )
    _require_result_ok(preflight, "netlist import preflight failed")

    runner = getattr(client, "ssh_runner", None)
    if runner is None:
        paths = _run_spicein_local(
            context,
            lib,
            cell,
            netlist_file,
            language=language,
            sim_name=sim_name,
            output_sim_name=output_sim_name,
            ref_libs=ref_libs,
            netlist_view=netlist_view,
            overwrite=overwrite,
            dev_map_file=dev_map_file,
            run_dir=resolved_run_dir,
            timeout=timeout,
        )
    else:
        paths = _run_spicein_remote(
            client,
            runner,
            context,
            lib,
            cell,
            netlist_file,
            language=language,
            sim_name=sim_name,
            output_sim_name=output_sim_name,
            ref_libs=ref_libs,
            netlist_view=netlist_view,
            overwrite=overwrite,
            dev_map_file=dev_map_file,
            run_dir=resolved_run_dir,
            timeout=timeout,
        )

    skill = schematic_import_netlist_skill(
        lib,
        cell,
        netlist_view=netlist_view,
        schematic_view=schematic_view,
        overwrite=overwrite,
        param_file=paths["param_file"],
        spicein_log_file=paths["spicein_log_file"],
    )
    result = client.execute_skill(skill, timeout=timeout)
    _require_result_ok(result, "netlist import conversion failed")
    return result


@dataclass(frozen=True)
class NetlistImportResult:
    """Structured summary of a netlist-to-schematic import attempt."""

    status: str
    lib: str | None = None
    cell: str | None = None
    param_file: str | None = None
    spicein_log_file: str | None = None
    conn2sch_log_file: str | None = None
    reason: str | None = None
    netlist_file: str | None = None
    netlist_view: str | None = None
    schematic_view: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the import completed successfully."""
        return self.status == "imported"


def parse_netlist_import_output(output: str) -> NetlistImportResult:
    """Parse common netlist import return payloads into a structured result."""
    stripped = output.strip()
    if not stripped:
        return NetlistImportResult(status="unknown", reason="empty output")

    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return NetlistImportResult(status="unknown", reason=f"invalid json: {exc}")
        return NetlistImportResult(
            status=str(payload.get("status", "unknown")),
            lib=payload.get("libName"),
            cell=payload.get("cellName"),
            param_file=payload.get("paramFile"),
            spicein_log_file=payload.get("spiceInLogFile"),
            conn2sch_log_file=payload.get("conn2schLogFile"),
            reason=payload.get("reason"),
            netlist_file=payload.get("netlistFile"),
            netlist_view=payload.get("netlistView"),
            schematic_view=payload.get("schematicView"),
        )

    parsed = parse_sexpr(stripped)
    if isinstance(parsed, list) and parsed and parsed[0] == "imported":
        values = ["" if value is None else str(value) for value in parsed]
        return NetlistImportResult(
            status="imported",
            lib=values[1] if len(values) > 1 else None,
            cell=values[2] if len(values) > 2 else None,
            param_file=values[3] if len(values) > 3 else None,
            spicein_log_file=values[4] if len(values) > 4 else None,
            conn2sch_log_file=values[5] if len(values) > 5 else None,
        )
    return NetlistImportResult(status="unknown", reason=stripped)


def classify_netlist_import_log(text: str) -> list[str]:
    """Classify common Spice In / conn2sch log failures."""
    lowered = text.lower()
    reasons: list[str] = []
    if any(marker in lowered for marker in ("unable to find master", "master cell", "devmap", "device mapping")):
        reasons.append("missing master or device mapping")
    if any(marker in lowered for marker in ("cannot open include", "can't open include", "no such file", "source netlist")):
        reasons.append("missing include or source netlist")
    if any(marker in lowered for marker in ("pin count", "terminal count", "pin mismatch")):
        reasons.append("pin mismatch")
    if "syntax error" in lowered or "parse error" in lowered:
        reasons.append("netlist syntax error")
    return reasons


def _schematic_import_preflight_skill(
    lib: str,
    cell: str,
    *,
    netlist_view: str,
    schematic_view: str,
    overwrite: bool,
) -> str:
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_netlist_view = escape_skill_string(netlist_view)
    escaped_schematic_view = escape_skill_string(schematic_view)
    return (
        "let((vbSchematicObj vbNetlistObj) "
        f'when("{escaped_netlist_view}" == "{escaped_schematic_view}" '
        'error("netlist and schematic views must differ")) '
        f'vbSchematicObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_schematic_view}") '
        "when(vbSchematicObj "
        f'if({_skill_bool(overwrite)} then ddReleaseObj(vbSchematicObj) '
        'else ddReleaseObj(vbSchematicObj) error("target schematic exists"))) '
        f'vbNetlistObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_netlist_view}") '
        "when(vbNetlistObj "
        f'if({_skill_bool(overwrite)} then ddReleaseObj(vbNetlistObj) '
        'else ddReleaseObj(vbNetlistObj) error("target netlist exists"))) '
        "t)"
    )


def _netlist_import_context_skill() -> str:
    return (
        'list(getWorkingDir() getShellEnvVar("PATH") getShellEnvVar("LD_LIBRARY_PATH") '
        'getShellEnvVar("LM_LICENSE_FILE") getShellEnvVar("CDS_LIC_FILE") cdsGetInstPath())'
    )


def _netlist_import_context(client: Any, *, timeout: int) -> dict[str, str]:
    result = client.execute_skill(_netlist_import_context_skill(), timeout=timeout)
    _require_result_ok(result, "netlist import environment discovery failed")
    parsed = parse_sexpr(_result_output(result))
    if not isinstance(parsed, list) or len(parsed) < 6:
        raise RuntimeError(f"unexpected netlist import context: {_result_output(result)}")
    keys = ["work_dir", "path", "ld_library_path", "lm_license_file", "cds_lic_file", "cds_inst_dir"]
    return {key: _context_value(value) for key, value in zip(keys, parsed)}


def _context_value(value: Any) -> str:
    return "" if value is None else str(value)


def _run_spicein_local(
    context: dict[str, str],
    lib: str,
    cell: str,
    netlist_file: str | Path,
    *,
    language: str,
    sim_name: str,
    output_sim_name: str,
    ref_libs: list[str] | tuple[str, ...],
    netlist_view: str,
    overwrite: bool,
    dev_map_file: str | Path | None,
    run_dir: str | Path,
    timeout: int,
) -> dict[str, str]:
    run_path = Path(run_dir).expanduser().resolve()
    run_path.mkdir(parents=True, exist_ok=True)
    netlist_path = _local_input_path(netlist_file, run_path)
    dev_map_path = "" if dev_map_file is None else _local_input_path(dev_map_file, run_path)
    paths = _import_paths(str(run_path))
    _write_spicein_stage(
        run_path / "spiceIn.il",
        run_path / "cds.lib",
        context,
        lib,
        cell,
        netlist_path,
        language=language,
        sim_name=sim_name,
        output_sim_name=output_sim_name,
        ref_libs=ref_libs,
        netlist_view=netlist_view,
        overwrite=overwrite,
        dev_map_file=dev_map_path,
        spicein_log_file=paths["spicein_log_file"],
    )
    command = [_spicein_executable(context), "-param", paths["param_file"]]
    result = subprocess.run(
        command,
        cwd=run_path,
        env=_local_spicein_env(context),
        timeout=timeout,
        capture_output=True,
        text=True,
    )
    stdout_file = run_path / "spiceIn.stdout"
    stdout_file.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(_spicein_failure_message(result.returncode, result.stdout, result.stderr))
    return paths


def _run_spicein_remote(
    client: Any,
    runner: Any,
    context: dict[str, str],
    lib: str,
    cell: str,
    netlist_file: str | Path,
    *,
    language: str,
    sim_name: str,
    output_sim_name: str,
    ref_libs: list[str] | tuple[str, ...],
    netlist_view: str,
    overwrite: bool,
    dev_map_file: str | Path | None,
    run_dir: str | Path,
    timeout: int,
) -> dict[str, str]:
    run_dir_text = str(run_dir)
    mkdir_result = runner.run_command(f"mkdir -p {shlex.quote(run_dir_text)}", timeout=timeout)
    _require_command_ok(mkdir_result, "cannot create netlist import run directory")
    paths = _import_paths(run_dir_text)
    inputs_dir = _input_stage_dir(run_dir_text)
    mkdir_inputs_result = runner.run_command(f"mkdir -p {shlex.quote(inputs_dir)}", timeout=timeout)
    _require_command_ok(mkdir_inputs_result, "cannot create netlist import input directory")
    remote_netlist_file = _stage_remote_input(
        client,
        runner,
        netlist_file,
        inputs_dir,
        role="netlist",
        timeout=timeout,
    )
    remote_dev_map_file = (
        ""
        if dev_map_file is None
        else _stage_remote_input(
            client,
            runner,
            dev_map_file,
            inputs_dir,
            role="devmap",
            timeout=timeout,
        )
    )
    with tempfile.TemporaryDirectory(prefix="vb-netlist-import-") as stage:
        stage_path = Path(stage)
        param_file = stage_path / "spiceIn.il"
        cds_lib = stage_path / "cds.lib"
        _write_spicein_stage(
            param_file,
            cds_lib,
            context,
            lib,
            cell,
            remote_netlist_file,
            language=language,
            sim_name=sim_name,
            output_sim_name=output_sim_name,
            ref_libs=ref_libs,
            netlist_view=netlist_view,
            overwrite=overwrite,
            dev_map_file=remote_dev_map_file,
            spicein_log_file=paths["spicein_log_file"],
        )
        _upload_required(client, param_file, paths["param_file"], timeout=timeout)
        _upload_required(client, cds_lib, f"{run_dir_text.rstrip('/')}/cds.lib", timeout=timeout)
    script = _remote_spicein_script(context, run_dir_text, paths["param_file"], paths["spicein_stdout_file"])
    result = runner.run_command(f"bash -lc {shlex.quote(script)}", timeout=timeout)
    _require_command_ok(result, "spiceIn failed")
    return paths


def _import_paths(run_dir: str) -> dict[str, str]:
    base = run_dir.rstrip("/")
    return {
        "param_file": f"{base}/spiceIn.il",
        "spicein_log_file": f"{base}/spiceIn.log",
        "spicein_stdout_file": f"{base}/spiceIn.stdout",
    }


def _input_stage_dir(run_dir: str) -> str:
    return f"{run_dir.rstrip('/')}/inputs"


def _resolve_import_run_dir(run_dir: str | Path | None, lib: str, cell: str) -> str:
    if run_dir is not None:
        return str(run_dir)
    return (
        f"/tmp/virtuoso_bridge_netlist_import_"
        f"{_safe_path_segment(lib)}_{_safe_path_segment(cell)}_{uuid.uuid4().hex}"
    )


def _safe_path_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe or "cell"


def _write_spicein_stage(
    param_file: Path,
    cds_lib_file: Path,
    context: dict[str, str],
    lib: str,
    cell: str,
    netlist_file: str,
    *,
    language: str,
    sim_name: str,
    output_sim_name: str,
    ref_libs: list[str] | tuple[str, ...],
    netlist_view: str,
    overwrite: bool,
    dev_map_file: str,
    spicein_log_file: str,
) -> None:
    param_file.write_text(
        _spicein_param_text(
            lib,
            cell,
            netlist_file,
            language=language,
            sim_name=sim_name,
            output_sim_name=output_sim_name,
            ref_libs=ref_libs,
            netlist_view=netlist_view,
            overwrite=overwrite,
            dev_map_file=dev_map_file,
            spicein_log_file=spicein_log_file,
        ),
        encoding="utf-8",
    )
    cds_lib_file.write_text(_staged_cds_lib_text(context), encoding="utf-8")


def _spicein_param_text(
    lib: str,
    cell: str,
    netlist_file: str,
    *,
    language: str,
    sim_name: str,
    output_sim_name: str,
    ref_libs: list[str] | tuple[str, ...],
    netlist_view: str,
    overwrite: bool,
    dev_map_file: str,
    spicein_log_file: str,
) -> str:
    overwrite_cells = "all" if overwrite else "none"
    rows = [
        "spiceInParams = list(nil",
        f'  \'language "{escape_skill_string(language)}"',
        f'  \'netlistFile "{escape_skill_string(netlist_file)}"',
        f'  \'importSubList "{escape_skill_string(cell)}"',
        f'  \'outputLib "{escape_skill_string(lib)}"',
        f'  \'refLibList "{escape_skill_string(" ".join(ref_libs))}"',
        f'  \'outputViewName "{escape_skill_string(netlist_view)}"',
        '  \'outputViewType "netlist"',
        f'  \'simName "{escape_skill_string(sim_name)}"',
        f'  \'outputSimName "{escape_skill_string(output_sim_name)}"',
        f'  \'overwriteCells "{overwrite_cells}"',
        f'  \'devMapFile "{escape_skill_string(dev_map_file)}"',
        '  \'masterCellForGnd "gnd"',
        f'  \'logFile "{escape_skill_string(spicein_log_file)}"',
        ")",
        "",
    ]
    return "\n".join(rows)


def _staged_cds_lib_text(context: dict[str, str]) -> str:
    work_dir = context.get("work_dir", "").rstrip("/")
    return f"INCLUDE {work_dir}/cds.lib\n" if work_dir else ""


def _local_input_path(path: str | Path, run_path: Path) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise RuntimeError(f"local input file not found: {path}")
    resolved = candidate.resolve()
    if resolved in _local_control_paths(run_path):
        raise RuntimeError(f"local input file conflicts with netlist import control file: {path}")
    return str(resolved)


def _local_control_paths(run_path: Path) -> set[Path]:
    return {
        (run_path / "spiceIn.il").resolve(),
        (run_path / "cds.lib").resolve(),
        (run_path / "spiceIn.log").resolve(),
        (run_path / "spiceIn.stdout").resolve(),
    }


def _stage_remote_input(
    client: Any,
    runner: Any,
    path: str | Path,
    inputs_dir: str,
    *,
    role: str,
    timeout: int,
) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        remote_path = str(path)
        if not PurePosixPath(remote_path).is_absolute():
            raise RuntimeError(f"remote input file must be absolute: {remote_path}")
        result = runner.run_command(f"test -f {shlex.quote(remote_path)}", timeout=timeout)
        if getattr(result, "returncode", 1) != 0:
            raise RuntimeError(f"remote input file not found: {remote_path}")
        return remote_path
    remote_path = f"{inputs_dir.rstrip('/')}/{_staged_input_name(role, candidate.name)}"
    _upload_required(client, candidate, remote_path, timeout=timeout)
    return remote_path


def _staged_input_name(role: str, source_name: str) -> str:
    return f"{_safe_path_segment(role)}_{_safe_path_segment(source_name)}"


def _upload_required(client: Any, local_path: Path, remote_path: str, *, timeout: int) -> None:
    result = client.upload_file(local_path, remote_path, timeout=timeout)
    _require_result_ok(result, f"failed to upload {local_path} to {remote_path}")


def _spicein_executable(context: dict[str, str]) -> str:
    cds_inst = context.get("cds_inst_dir", "").rstrip("/")
    return f"{cds_inst}/bin/spiceIn" if cds_inst else "spiceIn"


def _local_spicein_env(context: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(_spicein_env_values(context))
    return env


def _spicein_env_values(context: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, context_key in (
        ("PATH", "path"),
        ("LD_LIBRARY_PATH", "ld_library_path"),
        ("LM_LICENSE_FILE", "lm_license_file"),
        ("CDS_LIC_FILE", "cds_lic_file"),
    ):
        value = context.get(context_key, "")
        if value:
            values[key] = value
    cds_inst = context.get("cds_inst_dir", "")
    if cds_inst:
        values["CDSHOME"] = cds_inst
        values["CDS_INST_DIR"] = cds_inst
        values["IC_HOME"] = cds_inst
    return values


def _remote_spicein_script(
    context: dict[str, str],
    run_dir: str,
    param_file: str,
    stdout_file: str,
) -> str:
    exports = []
    env = _spicein_env_values(context)
    for key in ("PATH", "LD_LIBRARY_PATH", "LM_LICENSE_FILE", "CDS_LIC_FILE", "CDSHOME", "CDS_INST_DIR", "IC_HOME"):
        value = env.get(key, "")
        if value:
            exports.append(f"export {key}={shlex.quote(value)}")
    spicein = shlex.quote(_spicein_executable(context))
    stdout_q = shlex.quote(stdout_file)
    return "\n".join(
        [
            *exports,
            "export HOSTNAME=$(hostname 2>/dev/null || echo localhost)",
            f"cd {shlex.quote(run_dir)}",
            f"{spicein} -param {shlex.quote(param_file)} > {stdout_q} 2>&1",
            "rc=$?",
            f'if [ "$rc" -ne 0 ]; then tail -n 120 {stdout_q} >&2 2>/dev/null || true; fi',
            'exit "$rc"',
        ]
    )


def _require_command_ok(result: Any, default_error: str) -> None:
    if getattr(result, "returncode", 1) == 0:
        return
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    details = "\n".join(part.strip() for part in (stderr, stdout) if part and part.strip())
    message = f"{default_error} with exit code {getattr(result, 'returncode', 1)}"
    raise RuntimeError(message + (f": {details}" if details else ""))


def _spicein_failure_message(returncode: int, stdout: str, stderr: str) -> str:
    details = "\n".join(part.strip() for part in (stderr, stdout) if part and part.strip())
    return f"spiceIn failed with exit code {returncode}" + (f": {details}" if details else "")
