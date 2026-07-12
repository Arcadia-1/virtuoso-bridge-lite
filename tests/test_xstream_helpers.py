from __future__ import annotations

import re
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from virtuoso_bridge.virtuoso.layout import xstream
from virtuoso_bridge.virtuoso.layout.xstream import (
    XStreamExportRequest,
    XStreamLogResult,
    XStreamTranslatedStructure,
    _XStreamRequestResponse,
    _parse_xstream_request_response,
    parse_xstream_log,
    xstream_export_gds_skill,
)


_REQUEST_FIELDS = (
    "library",
    "top_cell",
    "view",
    "stream_file",
    "layer_map",
    "log_file",
    "run_dir",
)

_XSTREAM_FIELDS = (
    ("library", "library", "vbOldLibrary"),
    ("top_cell", "topCell", "vbOldTopCell"),
    ("view", "view", "vbOldView"),
    ("stream_file", "strmFile", "vbOldStreamFile"),
    ("layer_map", "layerMap", "vbOldLayerMap"),
    ("log_file", "logFile", "vbOldLogFile"),
    ("run_dir", "runDir", "vbOldRunDir"),
)

_LOG_CASES = {
    "incomplete": (
        "Product : Virtuoso(R) XStream Out\n"
        "Started at: SANITIZED_TIME\n"
        "WARNING: Synthetic export remains in progress.\n"
        "INFO (XSTRM-223): 1. Translating cellview "
        "WORK_LIB/TOP/layout as STRUCTURE TOP.\n"
    ),
    "malformed_completion": (
        "Product : Virtuoso(R) XStream Out\n"
        "Started at: SANITIZED_TIME\n"
        "INFO (XSTRM-234): Translation completed. unavailable error(s) "
        "and unavailable warning(s) found.\n"
    ),
    "nonzero_errors": (
        "Product : Virtuoso(R) XStream Out\n"
        "Started at: SANITIZED_TIME\n"
        "ERROR: Synthetic first export error.\n"
        "ERROR: Synthetic second export error.\n"
        'INFO (XSTRM-234): Translation completed. "2" error(s) and '
        "0 warning(s) found.\n"
    ),
    "success_with_warning": (
        "Product   : Virtuoso(R) XStream Out\n"
        "Started at: SANITIZED_TIME\n"
        "WARNING (XSTRM-333): Synthetic nonfatal warning.\n"
        "INFO (XSTRM-223): 1. Translating cellview "
        "REF_LIB/DEVICE/layout as STRUCTURE DEVICE.\n"
        "INFO (XSTRM-223): 2. Translating cellview "
        "WORK_LIB/TOP/layout as STRUCTURE TOP.\n"
        "INFO (XSTRM-234): Translation completed. '0' error(s) and "
        "'1' warning(s) found.\n"
    ),
    "terminal_failure": (
        "Product : Virtuoso(R) XStream Out\n"
        "Started at: SANITIZED_TIME\n"
        "INFO (XSTRM-273): Translation failed.\n"
    ),
}
_TRANSLATED_STRUCTURE_FIELDS = ("library", "cell", "view", "structure")
_LOG_RESULT_FIELDS = (
    "completed",
    "completion_line",
    "error_count",
    "warning_count",
    "translated_structures",
    "warnings",
    "errors",
    "terminal_failures",
    "parse_errors",
    "current_run_text",
)
_SENSITIVE_FIXTURE_PATTERNS = {
    "headers": re.compile(
        r"(?im)^[ \t]*(?:copyright|confidential|directory|platform|program|release)\b"
    ),
    "path": re.compile(
        r"(?i)(?<![A-Za-z0-9_.-])(?:/(?:[^/\s]+/)+[^/\s]*|[A-Z]:[\\/]|\\\\[^\\\s]+\\)"
    ),
    "user": re.compile(
        r"(?im)^[ \t]*(?:user(?:[ \t]*name)?|login|owner)[ \t]*[:=]"
    ),
    "host": re.compile(
        r"(?im)^[ \t]*(?:host(?:[ \t]*name)?|machine|server)[ \t]*[:=]"
    ),
    "time": re.compile(
        r"(?i)\b(?:19|20)\d{2}[-/]\d{2}[-/]\d{2}\b|"
        r"\b(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?\b|"
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
        r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b"
    ),
    "build": re.compile(
        r"(?i)\b(?:build|release|version)\b[ \t]*(?:[:=][ \t]*)?"
        r"[A-Za-z0-9][A-Za-z0-9._-]*"
    ),
    "internal IDs": re.compile(
        r"(?im)^\s*(?:job|process|pid|session|request|run)[ _-]*"
        r"(?:id|number)\s*[:=]|"
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    ),
    "window": re.compile(r"(?im)^\s*window(?:\s+id)?\s*[:=]"),
    "frame": re.compile(r"(?im)^\s*frame(?:\s+id)?\s*[:=]"),
    "coordinates": re.compile(
        r"(?im)^\s*(?:coordinates?|bbox|origin)\s*[:=]|"
        r"\bx\s*=\s*-?\d+(?:\.\d+)?\b.*\by\s*=\s*-?\d+(?:\.\d+)?\b|"
        r"\(\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*\)"
    ),
}


def _log_case(name: str) -> str:
    return _LOG_CASES[name]


def _request(**overrides: object) -> XStreamExportRequest:
    values: dict[str, object] = {
        "library": "demoLib",
        "top_cell": "nand2",
        "view": "layout",
        "stream_file": "output/nand2.gds",
        "layer_map": "maps/stream.map",
        "log_file": "logs/xstream.log",
        "run_dir": "runs/nand2",
    }
    values.update(overrides)
    return XStreamExportRequest(**values)  # type: ignore[arg-type]


def _assert_balanced_skill_parentheses(skill: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in skill:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            assert depth >= 0
    assert not in_string
    assert depth == 0


def test_xstream_export_request_is_frozen_with_exact_fields() -> None:
    request = _request()

    assert tuple(field.name for field in fields(request)) == _REQUEST_FIELDS
    with pytest.raises(FrozenInstanceError):
        request.library = "otherLib"  # type: ignore[misc]


def test_xstream_module_exports_public_request_renderer_and_log_parser() -> None:
    assert xstream.__all__ == [
        "XStreamExportRequest",
        "xstream_export_gds_skill",
        "XStreamTranslatedStructure",
        "XStreamLogResult",
        "parse_xstream_log",
    ]


@pytest.mark.parametrize("field_name", _REQUEST_FIELDS)
@pytest.mark.parametrize("invalid_value", ["", None])
def test_xstream_renderer_rejects_empty_or_non_string_fields(
    field_name: str,
    invalid_value: object,
) -> None:
    request = replace(_request(), **{field_name: invalid_value})

    with pytest.raises(ValueError, match=rf"^{field_name} must be a nonempty string$"):
        xstream_export_gds_skill(request)


def test_xstream_renderer_escapes_all_seven_request_values() -> None:
    request = _request(
        library='lib"\\one',
        top_cell='cell"\\two',
        view='view"\\three',
        stream_file='stream"\\four',
        layer_map='layer"\\five',
        log_file='log"\\six',
        run_dir='run"\\seven',
    )

    skill = xstream_export_gds_skill(request)

    for request_field, xstream_field, _old_variable in _XSTREAM_FIELDS:
        escaped = getattr(request, request_field).replace("\\", "\\\\").replace('"', '\\"')
        assert f'xstSetField("{xstream_field}" "{escaped}")' in skill


def test_xstream_renderer_captures_every_field_before_first_mutation() -> None:
    request = _request()

    skill = xstream_export_gds_skill(request)

    capture_positions = [
        skill.index(f'{old_variable} = xstGetField("{xstream_field}")')
        for _request_field, xstream_field, old_variable in _XSTREAM_FIELDS
    ]
    set_positions = [
        skill.index(
            f'xstSetField("{xstream_field}" "{getattr(request, request_field)}")'
        )
        for request_field, xstream_field, _old_variable in _XSTREAM_FIELDS
    ]
    assert max(capture_positions) < min(set_positions)


def test_xstream_renderer_suppresses_and_restores_completion_dialog() -> None:
    skill = xstream_export_gds_skill(_request())

    capture = (
        'vbOldShowCompletionMsgBox = '
        'xstGetField("showCompletionMsgBox")'
    )
    suppress = 'xstSetField("showCompletionMsgBox" "false")'
    translate = "xstOutDoTranslate()"
    restore = (
        'vbCleanup = errset(xstSetField("showCompletionMsgBox" '
        "vbOldShowCompletionMsgBox) nil)"
    )

    assert skill.count(capture) == 1
    assert skill.count(suppress) == 1
    assert skill.count(restore) == 1
    mutation_positions = [
        skill.index(f'xstSetField("{xstream_field}"')
        for _request_field, xstream_field, _old_variable in _XSTREAM_FIELDS
    ]
    mutation_positions.append(skill.index(suppress))
    assert skill.index(capture) < min(mutation_positions)
    assert skill.index(suppress) < skill.index(translate)
    assert skill.index(translate) < skill.index(restore)
    assert "failed to restore XStream field showCompletionMsgBox" in skill


def test_xstream_renderer_restores_exact_old_field_mapping_independently() -> None:
    skill = xstream_export_gds_skill(_request())

    assert skill.count("vbCleanup = errset(xstSetField(") == 8
    for _request_field, xstream_field, old_variable in _XSTREAM_FIELDS:
        restore = (
            f'vbCleanup = errset(xstSetField("{xstream_field}" '
            f"{old_variable}) nil)"
        )
        assert skill.count(restore) == 1


def test_xstream_renderer_does_not_treat_nil_cleanup_return_as_failure() -> None:
    skill = xstream_export_gds_skill(_request())

    assert "unless(vbCleanup " in skill
    assert "car(vbCleanup)" not in skill


def test_xstream_renderer_requires_all_three_xstream_apis() -> None:
    skill = xstream_export_gds_skill(_request())

    api_check = (
        "unless(and(isCallable('xstGetField) isCallable('xstSetField) "
        "isCallable('xstOutDoTranslate))"
    )
    assert api_check in skill
    assert skill.index(api_check) < skill.index('vbOldLibrary = xstGetField("library")')
    assert 'list("xstreamRequest" "failed"' in skill


def test_xstream_renderer_treats_nil_setter_and_launch_returns_as_success() -> None:
    skill = xstream_export_gds_skill(_request())

    assert "vbBodyAttempt = errset(progn(" in skill
    assert "xstOutDoTranslate()) nil)" in skill
    assert "if(vbBodyAttempt " in skill
    assert (
        'then list("xstreamRequest" "started" nil reverse(vbCleanupFailures))'
        in skill
    )
    assert "vbBodyAttempt && !vbCleanupFailures" not in skill
    assert "car(vbBodyAttempt)" not in skill
    assert "unless(xstSetField" not in skill
    assert "unless(xstOutDoTranslate" not in skill


def test_xstream_renderer_preserves_relative_execution_host_paths() -> None:
    request = _request(
        stream_file="../gds/out.gds",
        layer_map="config/layers.map",
        log_file="logs/export.log",
        run_dir="../runs/job-1",
    )

    skill = xstream_export_gds_skill(request)

    assert 'xstSetField("strmFile" "../gds/out.gds")' in skill
    assert 'xstSetField("layerMap" "config/layers.map")' in skill
    assert 'xstSetField("logFile" "logs/export.log")' in skill
    assert 'xstSetField("runDir" "../runs/job-1")' in skill


def test_parse_xstream_request_response_accepts_started_wire() -> None:
    response = _parse_xstream_request_response(
        '("xstreamRequest" "started" nil nil)'
    )

    assert response == _XStreamRequestResponse(
        state="started",
        body_error=None,
        cleanup_failures=(),
    )
    with pytest.raises(FrozenInstanceError):
        response.state = "failed"  # type: ignore[misc]


def test_parse_xstream_request_response_accepts_failed_body_and_cleanup_wire() -> None:
    response = _parse_xstream_request_response(
        r'("xstreamRequest" "failed" "launch \"bad\" at C:\\tmp" '
        r'("restore library" "restore C:\\run"))'
    )

    assert response == _XStreamRequestResponse(
        state="failed",
        body_error='launch "bad" at C:\\tmp',
        cleanup_failures=("restore library", "restore C:\\run"),
    )


def test_parse_xstream_request_response_accepts_started_cleanup_only_wire() -> None:
    response = _parse_xstream_request_response(
        '("xstreamRequest" "started" nil ("restore view"))'
    )

    assert response == _XStreamRequestResponse(
        state="started",
        body_error=None,
        cleanup_failures=("restore view",),
    )


@pytest.mark.parametrize(
    "output",
    [
        "",
        "nil",
        '("xstreamRequest" "started" nil nil',
        '("xstreamRequest" "started" nil nil) trailing',
        '("otherRequest" "started" nil nil)',
        '(xstreamRequest "started" nil nil)',
        '("xstreamRequest" started nil nil)',
        '("xstreamRequest" "started" nil)',
        '("xstreamRequest" "started" nil nil nil)',
        '("xstreamRequest" "queued" nil nil)',
        '("xstreamRequest" ("started") nil nil)',
        '("xstreamRequest" "started" "unexpected" nil)',
        '("xstreamRequest" "started" nil (restoreView))',
        '("xstreamRequest" "failed" nil nil)',
        '("xstreamRequest" "failed" nil ("restore view"))',
        '("xstreamRequest" "failed" launchFailed nil)',
        '("xstreamRequest" "failed" t nil)',
        '("xstreamRequest" "failed" "launch failed" "restore failed")',
        '("xstreamRequest" "failed" "launch failed" (t))',
        '("xstreamRequest" "failed" "launch failed" (nil))',
    ],
)
def test_parse_xstream_request_response_rejects_invalid_schema(output: str) -> None:
    with pytest.raises(ValueError, match="malformed XStream request response"):
        _parse_xstream_request_response(output)


def test_xstream_renderer_has_balanced_parentheses_outside_strings() -> None:
    skill = xstream_export_gds_skill(
        _request(log_file='logs/(quoted-"value").log')
    )

    assert "unwindProtect(" in skill
    _assert_balanced_skill_parentheses(skill)


def test_xstream_log_cases_are_sanitized() -> None:
    assert set(_LOG_CASES) == {
        "incomplete",
        "malformed_completion",
        "nonzero_errors",
        "success_with_warning",
        "terminal_failure",
    }
    for name, text in _LOG_CASES.items():
        for category, pattern in _SENSITIVE_FIXTURE_PATTERNS.items():
            assert pattern.search(text) is None, (
                f"{name} contains sensitive {category} data"
            )


@pytest.mark.parametrize(
    ("category", "sample"),
    [
        ("headers", "Release: IC-SYNTHETIC"),
        ("path", "/opt/synthetic/pdk/stream.map"),
        ("user", "Username: synthetic_user"),
        ("user", "User Name: synthetic_user"),
        ("host", "Host: synthetic.example"),
        ("host", "Host Name: synthetic.example"),
        ("headers", "Program: xstream"),
        ("headers", "Directory: synthetic/run"),
        ("time", "Started at: 2026-01-02 03:04:05"),
        ("build", "Build: 123456"),
        ("build", "Product metadata build 123456"),
        ("build", "Product metadata release IC-SYNTHETIC"),
        ("internal IDs", "Session ID: synthetic-session"),
        ("window", "Window ID: 0x123"),
        ("frame", "Frame: synthetic-frame"),
        ("coordinates", "Coordinates: (12, 34)"),
    ],
)
def test_xstream_log_sanitization_patterns_are_generic(
    category: str,
    sample: str,
) -> None:
    assert _SENSITIVE_FIXTURE_PATTERNS[category].search(sample) is not None


def test_parse_xstream_log_returns_success_details() -> None:
    text = _log_case("success_with_warning")

    result = parse_xstream_log(text)

    assert result == XStreamLogResult(
        completed=True,
        completion_line=(
            "INFO (XSTRM-234): Translation completed. '0' error(s) and "
            "'1' warning(s) found."
        ),
        error_count=0,
        warning_count=1,
        translated_structures=(
            XStreamTranslatedStructure(
                library="REF_LIB",
                cell="DEVICE",
                view="layout",
                structure="DEVICE",
            ),
            XStreamTranslatedStructure(
                library="WORK_LIB",
                cell="TOP",
                view="layout",
                structure="TOP",
            ),
        ),
        warnings=("WARNING (XSTRM-333): Synthetic nonfatal warning.",),
        errors=(),
        terminal_failures=(),
        parse_errors=(),
        current_run_text=text,
    )


def test_parse_xstream_log_returns_nonzero_completion_without_raising() -> None:
    result = parse_xstream_log(_log_case("nonzero_errors"))

    assert result.completed is True
    assert result.error_count == 2
    assert result.warning_count == 0
    assert result.errors == (
        "ERROR: Synthetic first export error.",
        "ERROR: Synthetic second export error.",
    )
    assert result.parse_errors == ()


def test_parse_xstream_log_detects_terminal_fixture_without_completion() -> None:
    terminal_line = "INFO (XSTRM-273): Translation failed."

    result = parse_xstream_log(_log_case("terminal_failure"))

    assert result.completed is False
    assert result.completion_line is None
    assert result.error_count is None
    assert result.warning_count is None
    assert result.terminal_failures == (terminal_line,)
    assert result.parse_errors == ()


@pytest.mark.parametrize(
    "terminal_line",
    [
        "INFO: xstrm-273 stopped the synthetic translation.",
        "INFO: Synthetic TRANSLATION FAILED before completion.",
        "INFO: Synthetic open_failed while writing output.",
    ],
)
def test_parse_xstream_log_detects_each_terminal_marker_independently(
    terminal_line: str,
) -> None:
    result = parse_xstream_log(terminal_line)

    assert result.terminal_failures == (terminal_line,)


@pytest.mark.parametrize(
    "line",
    [
        "INFO (XSTRM-2730): Synthetic adjacent status code.",
        "INFO: OPEN_FAILED_RECOVERED was handled.",
    ],
)
def test_parse_xstream_log_requires_terminal_marker_boundaries(line: str) -> None:
    result = parse_xstream_log(line)

    assert result.terminal_failures == ()


def test_parse_xstream_log_does_not_treat_structure_name_as_terminal() -> None:
    line = (
        "INFO (XSTRM-223): 1. Translating cellview WORK_LIB/TOP/layout "
        "as STRUCTURE OPEN_FAILED."
    )

    result = parse_xstream_log(line)

    assert result.translated_structures == (
        XStreamTranslatedStructure(
            library="WORK_LIB",
            cell="TOP",
            view="layout",
            structure="OPEN_FAILED",
        ),
    )
    assert result.terminal_failures == ()


def test_parse_xstream_log_reports_malformed_final_completion_counts() -> None:
    completion_line = (
        "INFO (XSTRM-234): Translation completed. unavailable error(s) and "
        "unavailable warning(s) found."
    )

    result = parse_xstream_log(_log_case("malformed_completion"))

    assert result.completed is True
    assert result.completion_line == completion_line
    assert result.error_count is None
    assert result.warning_count is None
    assert result.parse_errors == (
        f"malformed XStream completion counts: {completion_line}",
    )


def test_parse_xstream_log_ignores_error_diagnostic_quoting_completion() -> None:
    diagnostic_line = (
        'ERROR: Synthetic diagnostic quotes "INFO (XSTRM-234): Translation '
        'completed. 0 error(s) and 0 warning(s) found."'
    )

    result = parse_xstream_log(diagnostic_line)

    assert result.completed is False
    assert result.completion_line is None
    assert result.error_count is None
    assert result.warning_count is None
    assert result.parse_errors == ()


def test_parse_xstream_log_keeps_last_valid_full_completion_line() -> None:
    completion_line = (
        "INFO (XSTRM-234): Translation completed. '3' error(s) and "
        '"4" warning(s) found.'
    )
    diagnostic_line = (
        'ERROR: Later diagnostic quotes "Translation completed. 9 error(s) and '
        '9 warning(s) found."'
    )

    result = parse_xstream_log(f"{completion_line}\n{diagnostic_line}")

    assert result.completed is True
    assert result.completion_line == completion_line
    assert (result.error_count, result.warning_count) == (3, 4)
    assert result.parse_errors == ()


def test_parse_xstream_log_reports_oversized_completion_count_as_malformed() -> None:
    completion_line = (
        "INFO (XSTRM-234): Translation completed. "
        f"{'9' * 5000} error(s) and 0 warning(s) found."
    )

    result = parse_xstream_log(completion_line)

    assert result.completed is True
    assert result.completion_line == completion_line
    assert result.error_count is None
    assert result.warning_count is None
    assert result.parse_errors == (
        f"malformed XStream completion counts: {completion_line}",
    )


def test_parse_xstream_log_keeps_prefixed_completion_with_missing_counts() -> None:
    completion_line = (
        "INFO (XSTRM-234): Translation completed. counts unavailable."
    )

    result = parse_xstream_log(completion_line)

    assert result.completed is True
    assert result.completion_line == completion_line
    assert result.error_count is None
    assert result.warning_count is None
    assert result.parse_errors == (
        f"malformed XStream completion counts: {completion_line}",
    )


def test_parse_xstream_log_treats_missing_completion_as_incomplete(
) -> None:
    result = parse_xstream_log(_log_case("incomplete"))

    assert result.completed is False
    assert result.completion_line is None
    assert result.error_count is None
    assert result.warning_count is None
    assert result.translated_structures == (
        XStreamTranslatedStructure("WORK_LIB", "TOP", "layout", "TOP"),
    )
    assert result.parse_errors == ()


def test_parse_xstream_log_selects_newest_product_run_and_retains_headers() -> None:
    previous_run = "\n".join(
        [
            "Product : Virtuoso(R) XStream Out",
            "Started at: SANITIZED_TIME_OLD",
            "ERROR: Old run error.",
            "INFO (XSTRM-234): Translation completed. 1 error(s) and "
            "0 warning(s) found.",
        ]
    )
    current_run = (
        "\n".join(
            [
                "Product   : Virtuoso(R) XStream Out",
                "Synthetic current-run header.",
                "Started at: SANITIZED_TIME_NEW",
                "WARNING: New run warning.",
                "INFO (XSTRM-234): Translation completed. 0 error(s) and "
                "1 warning(s) found.",
            ]
        )
        + "\n\n"
    )
    text = f"{previous_run}\n{current_run}"

    result = parse_xstream_log(text)

    assert result.error_count == 0
    assert result.warning_count == 1
    assert result.errors == ()
    assert result.warnings == ("WARNING: New run warning.",)
    assert result.current_run_text == current_run
    assert result.current_run_text.startswith(
        "Product   : Virtuoso(R) XStream Out\nSynthetic current-run header.\n"
        "Started at: SANITIZED_TIME_NEW"
    )
    assert "Old run error" not in result.current_run_text


@pytest.mark.parametrize(
    "product_header",
    [
        "Product : Virtuoso(R) XStream Out",
        "Product   : Virtuoso(R) XStream Out",
        "Product\t:\tVirtuoso(R) XStream Out",
        "Product: Virtuoso(R) XStream Out",
    ],
)
def test_parse_xstream_log_accepts_product_header_whitespace(
    product_header: str,
) -> None:
    expected = f"{product_header}\nStarted at: SANITIZED_TIME\n"
    text = f"Synthetic previous content.\n{expected}"

    result = parse_xstream_log(text)

    assert result.current_run_text == expected


def test_parse_xstream_log_falls_back_to_last_started_anchor() -> None:
    previous_run = "\n".join(
        [
            "Started at: SANITIZED_TIME_OLD",
            "ERROR: Old run error.",
        ]
    )
    current_run = (
        "Started at: SANITIZED_TIME_NEW\nWARNING: New run warning.\n\n"
    )
    text = f"{previous_run}\n{current_run}"

    result = parse_xstream_log(text)

    assert result.current_run_text == current_run
    assert result.errors == ()
    assert result.warnings == ("WARNING: New run warning.",)


def test_started_fallback_ignores_midline_diagnostic_text() -> None:
    previous_run = (
        "Started at: SANITIZED_TIME_OLD\n"
        "ERROR: Old run error.\n"
    )
    current_run = (
        "sTaRtEd\tAt : SANITIZED_TIME_NEW\n"
        "WARNING: Diagnostic mentions Started at: NOT_ANCHOR\n"
    )

    result = parse_xstream_log(f"{previous_run}{current_run}")

    assert result.current_run_text == current_run
    assert result.errors == ()
    assert result.warnings == (
        "WARNING: Diagnostic mentions Started at: NOT_ANCHOR",
    )


def test_parse_xstream_log_preserves_exact_full_text_without_run_anchor() -> None:
    text = "\n  WARNING: Synthetic warning.  \n\nERROR: Synthetic error.\n"

    result = parse_xstream_log(text)

    assert result.current_run_text == text
    assert result.warnings == ("WARNING: Synthetic warning.",)
    assert result.errors == ("ERROR: Synthetic error.",)


@pytest.mark.parametrize(
    ("first_completion", "final_completion", "counts", "parse_errors"),
    [
        (
            "INFO (XSTRM-234): Translation completed. unavailable error(s) "
            "and unavailable warning(s) found.",
            "INFO (XSTRM-234): Translation completed. '3' error(s) and "
            '"4" warning(s) found.',
            (3, 4),
            (),
        ),
        (
            "INFO (XSTRM-234): Translation completed. 0 error(s) and "
            "0 warning(s) found.",
            "INFO (XSTRM-234): Translation completed. unavailable error(s) "
            "and unavailable warning(s) found.",
            (None, None),
            (
                "malformed XStream completion counts: INFO (XSTRM-234): "
                "Translation completed. unavailable error(s) and unavailable "
                "warning(s) found.",
            ),
        ),
    ],
)
def test_parse_xstream_log_uses_final_completion_marker(
    first_completion: str,
    final_completion: str,
    counts: tuple[int | None, int | None],
    parse_errors: tuple[str, ...],
) -> None:
    result = parse_xstream_log(f"{first_completion}\n{final_completion}")

    assert result.completed is True
    assert result.completion_line == final_completion
    assert (result.error_count, result.warning_count) == counts
    assert result.parse_errors == parse_errors


def test_parse_xstream_completion_is_not_an_error_severity_line() -> None:
    completion_line = (
        "INFO (XSTRM-234): Translation completed. 2 error(s) and 0 warning(s) found."
    )

    result = parse_xstream_log(completion_line)

    assert result.error_count == 2
    assert result.errors == ()


def test_parse_xstream_unmapped_and_unresolved_are_not_terminal() -> None:
    text = "\n".join(
        [
            "INFO: Synthetic layer is unmapped.",
            "WARNING: Synthetic reference is unresolved.",
        ]
    )

    result = parse_xstream_log(text)

    assert result.terminal_failures == ()


@pytest.mark.parametrize(
    ("line", "collection_name"),
    [
        ("WARNING Synthetic warning.", "warnings"),
        ("WARNING: Synthetic warning.", "warnings"),
        ("WARNING(XSTRM-001): Synthetic warning.", "warnings"),
        ("*WARNING* Synthetic warning.", "warnings"),
        ("ERROR Synthetic error.", "errors"),
        ("ERROR: Synthetic error.", "errors"),
        ("ERROR(XSTRM-001): Synthetic error.", "errors"),
        ("*ERROR* Synthetic error.", "errors"),
    ],
)
def test_parse_xstream_log_recognizes_supported_severity_prefixes(
    line: str,
    collection_name: str,
) -> None:
    result = parse_xstream_log(line)

    assert getattr(result, collection_name) == (line,)


def test_parse_xstream_log_preserves_structure_order_and_duplicates() -> None:
    line = (
        "INFO (XSTRM-223): 1. Translating cellview WORK_LIB/TOP/layout "
        "as STRUCTURE TOP."
    )

    result = parse_xstream_log(f"{line}\n{line}")

    structure = XStreamTranslatedStructure("WORK_LIB", "TOP", "layout", "TOP")
    assert result.translated_structures == (structure, structure)


def test_parse_xstream_log_empty_text_returns_empty_incomplete_result() -> None:
    result = parse_xstream_log("")

    assert result == XStreamLogResult(
        completed=False,
        completion_line=None,
        error_count=None,
        warning_count=None,
        translated_structures=(),
        warnings=(),
        errors=(),
        terminal_failures=(),
        parse_errors=(),
        current_run_text="",
    )


def test_parse_xstream_log_rejects_non_string_input() -> None:
    with pytest.raises(TypeError):
        parse_xstream_log(None)  # type: ignore[arg-type]


def test_xstream_log_models_are_frozen_with_exact_fields_and_tuple_results() -> None:
    assert tuple(field.name for field in fields(XStreamTranslatedStructure)) == (
        _TRANSLATED_STRUCTURE_FIELDS
    )
    assert tuple(field.name for field in fields(XStreamLogResult)) == _LOG_RESULT_FIELDS

    structure = XStreamTranslatedStructure("WORK_LIB", "TOP", "layout", "TOP")
    with pytest.raises(FrozenInstanceError):
        structure.cell = "OTHER"  # type: ignore[misc]

    result = parse_xstream_log(_log_case("success_with_warning"))
    for collection_name in (
        "translated_structures",
        "warnings",
        "errors",
        "terminal_failures",
        "parse_errors",
    ):
        assert isinstance(getattr(result, collection_name), tuple)
    with pytest.raises(FrozenInstanceError):
        result.completed = False  # type: ignore[misc]
