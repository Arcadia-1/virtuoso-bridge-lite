from __future__ import annotations

from virtuoso_bridge.virtuoso.schematic import (
    NetlistImportResult,
    classify_netlist_import_log,
    parse_netlist_import_output,
)


def test_parse_netlist_import_output_from_skill_list() -> None:
    result = parse_netlist_import_output(
        '("imported" "demoLib" "nand2" "/run/spiceIn.il" "/run/spiceIn.log" "/run/conn2sch.stdout")'
    )

    assert result == NetlistImportResult(
        status="imported",
        lib="demoLib",
        cell="nand2",
        param_file="/run/spiceIn.il",
        spicein_log_file="/run/spiceIn.log",
        conn2sch_log_file="/run/conn2sch.stdout",
        reason=None,
    )
    assert result.ok is True


def test_parse_netlist_import_output_decodes_skill_string_escapes() -> None:
    result = parse_netlist_import_output(
        r'("imported" "demo\"Lib" "nand\\2" "/run/a\tb/spiceIn.il")'
    )

    assert result.status == "imported"
    assert result.lib == 'demo"Lib'
    assert result.cell == "nand\\2"
    assert result.param_file == "/run/a\tb/spiceIn.il"


def test_parse_netlist_import_output_from_json_error() -> None:
    result = parse_netlist_import_output(
        '{"status":"error","reason":"spicein_failed","libName":"demoLib",'
        '"cellName":"nand2","netlistFile":"/tmp/nand2.scs",'
        '"netlistView":"netlist","schematicView":"schematic"}'
    )

    assert result.ok is False
    assert result.status == "error"
    assert result.reason == "spicein_failed"
    assert result.lib == "demoLib"
    assert result.cell == "nand2"


def test_classify_netlist_import_log_detects_common_failures() -> None:
    text = "\n".join(
        [
            "ERROR: Unable to find master cell for subckt sky130_fd_pr__nfet_01v8.",
            "Cannot open include file models.scs.",
            "WARNING: pin count mismatch for instance X1.",
        ]
    )

    assert classify_netlist_import_log(text) == [
        "missing master or device mapping",
        "missing include or source netlist",
        "pin mismatch",
    ]
