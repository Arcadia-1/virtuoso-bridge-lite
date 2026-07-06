"""Helpers for exporting schematic netlists through Virtuoso's netlister."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

from virtuoso_bridge.models import ExecutionStatus
from virtuoso_bridge.virtuoso.ops import escape_skill_string

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
    run_dir: str | Path = "/tmp/virtuoso_bridge_netlist_import",
) -> str:
    """Build SKILL to import a netlist and convert it to a schematic view.

    The generated flow uses Cadence Spice In to create an intermediate
    netlist view, then converts that connectivity view into a schematic using
    ``conn2Sch`` when available, with the ``conn2sch`` command as fallback.
    """
    escaped_lib = escape_skill_string(lib)
    escaped_cell = escape_skill_string(cell)
    escaped_netlist_file = escape_skill_string(str(netlist_file))
    escaped_language = escape_skill_string(language)
    escaped_sim_name = escape_skill_string(sim_name)
    escaped_output_sim_name = escape_skill_string(output_sim_name)
    escaped_ref_libs = escape_skill_string(" ".join(ref_libs))
    escaped_netlist_view = escape_skill_string(netlist_view)
    escaped_schematic_view = escape_skill_string(schematic_view)
    escaped_dev_map = escape_skill_string("" if dev_map_file is None else str(dev_map_file))
    escaped_run_dir = escape_skill_string(str(run_dir))
    overwrite_cells = "all" if overwrite else "none"

    return (
        "let((vbRunDir vbParamFile vbSpiceInLog vbSpiceInStdout vbConn2SchLog "
        "vbOut vbSchematicObj vbNetlistObj vbSpiceOk vbConnOk) "
        f'when("{escaped_netlist_view}" == "{escaped_schematic_view}" '
        'error("netlist and schematic views must differ")) '
        f'vbRunDir = "{escaped_run_dir}" '
        "unless(isDir(vbRunDir) || createDirHier(vbRunDir) error(\"cannot create run directory\")) "
        f'vbParamFile = strcat("{escaped_run_dir}" "/spiceIn.il") '
        f'vbSpiceInLog = strcat("{escaped_run_dir}" "/spiceIn.log") '
        f'vbSpiceInStdout = strcat("{escaped_run_dir}" "/spiceIn.stdout") '
        f'vbConn2SchLog = strcat("{escaped_run_dir}" "/conn2sch.stdout") '
        f'vbSchematicObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_schematic_view}") '
        "when(vbSchematicObj "
        f'if({_skill_bool(overwrite)} then '
        'unless(ddDeleteObj(vbSchematicObj) error("target schematic delete failed")) '
        'else ddReleaseObj(vbSchematicObj) error("target schematic exists"))) '
        f'vbNetlistObj = ddGetObj("{escaped_lib}" "{escaped_cell}" "{escaped_netlist_view}") '
        "when(vbNetlistObj "
        f'if({_skill_bool(overwrite)} then '
        'unless(ddDeleteObj(vbNetlistObj) error("target netlist delete failed")) '
        'else ddReleaseObj(vbNetlistObj) error("target netlist exists"))) '
        "vbOut = outfile(vbParamFile \"w\") "
        'unless(vbOut error("cannot open spiceIn parameter file")) '
        'fprintf(vbOut "spiceInParams = list(nil\\n") '
        f"fprintf(vbOut \"  'language %L\\n\" \"{escaped_language}\") "
        f"fprintf(vbOut \"  'netlistFile %L\\n\" \"{escaped_netlist_file}\") "
        f"fprintf(vbOut \"  'importSubList %L\\n\" \"{escaped_cell}\") "
        f"fprintf(vbOut \"  'outputLib %L\\n\" \"{escaped_lib}\") "
        f"fprintf(vbOut \"  'refLibList %L\\n\" \"{escaped_ref_libs}\") "
        f"fprintf(vbOut \"  'outputViewName %L\\n\" \"{escaped_netlist_view}\") "
        "fprintf(vbOut \"  'outputViewType %L\\n\" \"netlist\") "
        f"fprintf(vbOut \"  'simName %L\\n\" \"{escaped_sim_name}\") "
        f"fprintf(vbOut \"  'outputSimName %L\\n\" \"{escaped_output_sim_name}\") "
        f"fprintf(vbOut \"  'overwriteCells %L\\n\" \"{overwrite_cells}\") "
        f"fprintf(vbOut \"  'devMapFile %L\\n\" \"{escaped_dev_map}\") "
        "fprintf(vbOut \"  'masterCellForGnd %L\\n\" \"gnd\") "
        "fprintf(vbOut \"  'logFile %L\\n\" vbSpiceInLog) "
        'fprintf(vbOut ")\\n") '
        "close(vbOut) "
        "unless(isCallable('system) error(\"system API unavailable\")) "
        "vbSpiceOk = system(strcat(\"cd \" vbRunDir \" && spiceIn -param \" vbParamFile "
        "\" > \" vbSpiceInStdout \" 2>&1\")) "
        "unless(or(vbSpiceOk == 0 vbSpiceOk == t) error(\"spiceIn failed\")) "
        "vbConnOk = nil "
        "when(isCallable('conn2Sch) "
        f'vbConnOk = errset(conn2Sch("{escaped_lib}" "{escaped_cell}" "{escaped_netlist_view}" '
        f'?destLibName "{escaped_lib}" ?destCellName "{escaped_cell}" '
        f'?destViewName "{escaped_schematic_view}" ?block t) nil)) '
        "unless(vbConnOk && car(vbConnOk) "
        "vbConnOk = system(strcat(\"cd \" vbRunDir "
        f'" && conn2sch -lib {escaped_lib} -cell {escaped_cell} -view {escaped_netlist_view} '
        f'-destlib {escaped_lib} -destview {escaped_schematic_view}" '
        '" > " vbConn2SchLog " 2>&1")) '
        'unless(or(vbConnOk == 0 vbConnOk == t) error("conn2sch failed"))) '
        f'list("imported" "{escaped_lib}" "{escaped_cell}" vbParamFile vbSpiceInLog vbConn2SchLog))'
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
    run_dir: str | Path = "/tmp/virtuoso_bridge_netlist_import",
    timeout: int = 300,
) -> Any:
    """Import a netlist into a schematic view by executing generated SKILL."""
    skill = schematic_import_netlist_skill(
        lib,
        cell,
        netlist_file,
        language=language,
        sim_name=sim_name,
        output_sim_name=output_sim_name,
        ref_libs=ref_libs,
        netlist_view=netlist_view,
        schematic_view=schematic_view,
        overwrite=overwrite,
        dev_map_file=dev_map_file,
        run_dir=run_dir,
    )
    return client.execute_skill(skill, timeout=timeout)
