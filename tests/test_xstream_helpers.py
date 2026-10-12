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
_MALFORMED_COMPLETION = (
    "INFO (XSTRM-234): Translation completed. unavailable error(s) and "
    "unavailable warning(s) found."
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
_SENSITIVE_SAMPLES = (
    ("headers", "Release: IC-SYNTHETIC"),
    ("headers", "Program: xstream"),
    ("headers", "Directory: synthetic/run"),
    ("path", "/opt/synthetic/pdk/stream.map"),
    ("user", "Username: synthetic_user"),
    ("user", "User Name: synthetic_user"),
    ("host", "Host: synthetic.example"),
    ("host", "Host Name: synthetic.example"),
    ("time", "Started at: 2026-01-02 03:04:05"),
    ("build", "Build: 123456"),
    ("build", "Product metadata build 123456"),
    ("build", "Product metadata release IC-SYNTHETIC"),
    ("internal IDs", "Session ID: synthetic-session"),
    ("window", "Window ID: 0x123"),
    ("frame", "Frame: synthetic-frame"),
    ("coordinates", "Coordinates: (12, 34)"),
)


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


def test_xstream_public_models_are_frozen_with_exact_fields() -> None:
    assert xstream.__all__ == [
        "XStreamExportRequest",
        "xstream_export_gds_skill",
        "XStreamTranslatedStructure",
        "XStreamLogResult",
        "parse_xstream_log",
    ]
    assert tuple(field.name for field in fields(XStreamExportRequest)) == (
        _REQUEST_FIELDS
    )
    assert tuple(field.name for field in fields(XStreamTranslatedStructure)) == (
        "library",
        "cell",
        "view",
        "structure",
    )
    assert tuple(field.name for field in fields(XStreamLogResult)) == (
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
    structure = XStreamTranslatedStructure("WORK_LIB", "TOP", "layout", "TOP")
    log = parse_xstream_log(_LOG_CASES["success_with_warning"])
    for frozen, attribute in (
        (_request(), "library"),
        (structure, "cell"),
        (log, "completed"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(frozen, attribute, None)


@pytest.mark.parametrize("field_name", _REQUEST_FIELDS)
@pytest.mark.parametrize("invalid_value", ["", None])
def test_xstream_renderer_rejects_empty_or_non_string_fields(
    field_name: str,
    invalid_value: object,
) -> None:
    request = replace(_request(), **{field_name: invalid_value})

    with pytest.raises(ValueError, match=rf"^{field_name} must be a nonempty string$"):
        xstream_export_gds_skill(request)


def test_xstream_renderer_escapes_values_and_keeps_relative_host_paths() -> None:
    request = _request(
        library='lib"\\one',
        top_cell='cell"\\two',
        view='view"\\three',
        stream_file='../gds/"out"\\four.gds',
        layer_map='config/layer"\\five.map',
        log_file='logs/(quoted-"value")\\six.log',
        run_dir='../runs/job"\\seven',
    )

    skill = xstream_export_gds_skill(request)

    for request_field, xstream_field, _old_variable in _XSTREAM_FIELDS:
        value = getattr(request, request_field)
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        assert f'xstSetField("{xstream_field}" "{escaped}")' in skill
    assert "unwindProtect(" in skill
    _assert_balanced_skill_parentheses(skill)


def test_xstream_renderer_captures_mutates_translates_then_restores() -> None:
    request = _request()

    skill = xstream_export_gds_skill(request)

    api_check = (
        "unless(and(isCallable('xstGetField) isCallable('xstSetField) "
        "isCallable('xstOutDoTranslate))"
    )
    restored_fields = tuple((xstream_field, old) for _, xstream_field, old in _XSTREAM_FIELDS)
    captures = [
        skill.index(f'{old} = xstGetField("{field}")')
        for field, old in restored_fields
    ]
    setters = [
        skill.index(f'xstSetField("{xstream_field}" "{getattr(request, name)}")')
        for name, xstream_field, _old in _XSTREAM_FIELDS
    ]
    setters.append(skill.index('xstSetField("showCompletionMsgBox" "false")'))
    translate = skill.index("xstOutDoTranslate()")
    assert skill.index(api_check) < min(captures)
    assert max(captures) < min(setters)
    assert max(setters) < translate
    assert skill.count("vbCleanup = errset(xstSetField(") == len(restored_fields)
    for field, old in restored_fields:
        restore = f'vbCleanup = errset(xstSetField("{field}" {old}) nil)'
        assert skill.count(restore) == 1
        assert translate < skill.index(restore)
        assert f"failed to restore XStream field {field}" in skill

    # IC6.1.8 reads showCompletionMsgBox when the non-blocking translation
    # finishes, after this SKILL has returned; restoring it here raised the
    # modal completion box, so it stays off.
    assert skill.count("showCompletionMsgBox") == 1
    assert "xstShowCompletionMsgBox" not in skill


def test_xstream_renderer_reports_wire_state_from_errset_results() -> None:
    skill = xstream_export_gds_skill(_request())

    assert "vbBodyAttempt = errset(progn(" in skill
    assert "xstOutDoTranslate()) nil)" in skill
    assert "unless(vbCleanup " in skill
    assert (
        'if(vbBodyAttempt then list("xstreamRequest" "started" nil '
        "reverse(vbCleanupFailures))"
    ) in skill
    assert 'else list("xstreamRequest" "failed" vbBodyError' in skill
    for nil_return_misread in (
        "car(vbCleanup)",
        "car(vbBodyAttempt)",
        "vbBodyAttempt && !vbCleanupFailures",
        "unless(xstSetField",
        "unless(xstOutDoTranslate",
    ):
        assert nil_return_misread not in skill


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            '("xstreamRequest" "started" nil nil)',
            _XStreamRequestResponse("started", None, ()),
        ),
        (
            '("xstreamRequest" "started" nil ("restore view"))',
            _XStreamRequestResponse("started", None, ("restore view",)),
        ),
        (
            r'("xstreamRequest" "failed" "launch \"bad\" at C:\\tmp" '
            r'("restore library" "restore C:\\run"))',
            _XStreamRequestResponse(
                "failed",
                'launch "bad" at C:\\tmp',
                ("restore library", "restore C:\\run"),
            ),
        ),
    ],
)
def test_parse_xstream_request_response_accepts_canonical_wire(
    output: str,
    expected: _XStreamRequestResponse,
) -> None:
    response = _parse_xstream_request_response(output)

    assert response == expected
    with pytest.raises(FrozenInstanceError):
        response.state = "failed"  # type: ignore[misc]


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


def test_xstream_log_fixtures_are_sanitized_by_generic_patterns() -> None:
    for category, sample in _SENSITIVE_SAMPLES:
        assert _SENSITIVE_FIXTURE_PATTERNS[category].search(sample), sample
    for name, text in _LOG_CASES.items():
        for category, pattern in _SENSITIVE_FIXTURE_PATTERNS.items():
            assert pattern.search(text) is None, (
                f"{name} contains sensitive {category} data"
            )


def test_parse_xstream_log_returns_success_details() -> None:
    text = _LOG_CASES["success_with_warning"]

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
            XStreamTranslatedStructure("REF_LIB", "DEVICE", "layout", "DEVICE"),
            XStreamTranslatedStructure("WORK_LIB", "TOP", "layout", "TOP"),
        ),
        warnings=("WARNING (XSTRM-333): Synthetic nonfatal warning.",),
        errors=(),
        terminal_failures=(),
        parse_errors=(),
        current_run_text=text,
    )


@pytest.mark.parametrize(
    ("case", "completion_line", "counts", "errors", "terminal_failures"),
    [
        (
            "nonzero_errors",
            'INFO (XSTRM-234): Translation completed. "2" error(s) and '
            "0 warning(s) found.",
            (2, 0),
            (
                "ERROR: Synthetic first export error.",
                "ERROR: Synthetic second export error.",
            ),
            (),
        ),
        (
            "terminal_failure",
            None,
            (None, None),
            (),
            ("INFO (XSTRM-273): Translation failed.",),
        ),
        ("malformed_completion", _MALFORMED_COMPLETION, (None, None), (), ()),
        ("incomplete", None, (None, None), (), ()),
    ],
)
def test_parse_xstream_log_summarizes_failure_fixtures_without_raising(
    case: str,
    completion_line: str | None,
    counts: tuple[int | None, int | None],
    errors: tuple[str, ...],
    terminal_failures: tuple[str, ...],
) -> None:
    result = parse_xstream_log(_LOG_CASES[case])

    assert result.completed is (completion_line is not None)
    assert result.completion_line == completion_line
    assert (result.error_count, result.warning_count) == counts
    assert result.errors == errors
    assert result.terminal_failures == terminal_failures
    malformed = completion_line is not None and counts == (None, None)
    assert result.parse_errors == (
        (f"malformed XStream completion counts: {completion_line}",)
        if malformed
        else ()
    )


@pytest.mark.parametrize(
    ("line", "terminal"),
    [
        ("INFO: xstrm-273 stopped the synthetic translation.", True),
        ("INFO: Synthetic TRANSLATION FAILED before completion.", True),
        ("INFO: Synthetic open_failed while writing output.", True),
        ("INFO (XSTRM-2730): Synthetic adjacent status code.", False),
        ("INFO: OPEN_FAILED_RECOVERED was handled.", False),
        ("INFO: Synthetic layer is unmapped.", False),
        ("WARNING: Synthetic reference is unresolved.", False),
        (
            "INFO (XSTRM-223): 1. Translating cellview WORK_LIB/TOP/layout "
            "as STRUCTURE OPEN_FAILED.",
            False,
        ),
    ],
)
def test_parse_xstream_log_detects_bounded_terminal_markers(
    line: str,
    terminal: bool,
) -> None:
    result = parse_xstream_log(line)

    assert result.terminal_failures == ((line,) if terminal else ())


_COMPLETION_0_0 = (
    "INFO (XSTRM-234): Translation completed. 0 error(s) and 0 warning(s) found."
)
_COMPLETION_3_4 = (
    "INFO (XSTRM-234): Translation completed. '3' error(s) and "
    '"4" warning(s) found.'
)


@pytest.mark.parametrize(
    ("text", "completion_line", "counts"),
    [
        (
            'ERROR: Synthetic diagnostic quotes "INFO (XSTRM-234): Translation '
            'completed. 0 error(s) and 0 warning(s) found."',
            None,
            (None, None),
        ),
        (
            f"{_COMPLETION_3_4}\n"
            'ERROR: Later diagnostic quotes "Translation completed. 9 error(s) '
            'and 9 warning(s) found."',
            _COMPLETION_3_4,
            (3, 4),
        ),
        (f"{_MALFORMED_COMPLETION}\n{_COMPLETION_3_4}", _COMPLETION_3_4, (3, 4)),
        (
            f"{_COMPLETION_0_0}\n{_MALFORMED_COMPLETION}",
            _MALFORMED_COMPLETION,
            (None, None),
        ),
        (
            "INFO (XSTRM-234): Translation completed. "
            f"{'9' * 5000} error(s) and 0 warning(s) found.",
            "INFO (XSTRM-234): Translation completed. "
            f"{'9' * 5000} error(s) and 0 warning(s) found.",
            (None, None),
        ),
        (
            "INFO (XSTRM-234): Translation completed. counts unavailable.",
            "INFO (XSTRM-234): Translation completed. counts unavailable.",
            (None, None),
        ),
        (
            "INFO (XSTRM-234): Translation completed. 2 error(s) and "
            "0 warning(s) found.",
            "INFO (XSTRM-234): Translation completed. 2 error(s) and "
            "0 warning(s) found.",
            (2, 0),
        ),
    ],
    ids=[
        "quoted-in-error",
        "last-full-line",
        "final-valid",
        "final-malformed",
        "oversized-count",
        "missing-counts",
        "completion-is-not-error-line",
    ],
)
def test_parse_xstream_log_uses_last_full_completion_line(
    text: str,
    completion_line: str | None,
    counts: tuple[int | None, int | None],
) -> None:
    result = parse_xstream_log(text)

    assert result.completed is (completion_line is not None)
    assert result.completion_line == completion_line
    assert (result.error_count, result.warning_count) == counts
    malformed = completion_line is not None and counts == (None, None)
    assert result.parse_errors == (
        (f"malformed XStream completion counts: {completion_line}",)
        if malformed
        else ()
    )
    assert result.errors == tuple(
        line for line in text.splitlines() if line.startswith("ERROR")
    )


@pytest.mark.parametrize(
    ("previous_run", "current_run"),
    [
        (
            "Product : Virtuoso(R) XStream Out\nStarted at: SANITIZED_TIME_OLD\n"
            "ERROR: Old run error.\n"
            "INFO (XSTRM-234): Translation completed. 1 error(s) and "
            "0 warning(s) found.\n",
            "Product   : Virtuoso(R) XStream Out\nSynthetic current-run header.\n"
            "Started at: SANITIZED_TIME_NEW\nWARNING: New run warning.\n"
            "INFO (XSTRM-234): Translation completed. 0 error(s) and "
            "1 warning(s) found.\n\n",
        ),
        *(
            (
                "Synthetic previous content.\nERROR: Old run error.\n",
                f"{header}\nStarted at: SANITIZED_TIME\n",
            )
            for header in (
                "Product : Virtuoso(R) XStream Out",
                "Product\t:\tVirtuoso(R) XStream Out",
                "Product: Virtuoso(R) XStream Out",
            )
        ),
        (
            "Started at: SANITIZED_TIME_OLD\nERROR: Old run error.\n",
            "Started at: SANITIZED_TIME_NEW\nWARNING: New run warning.\n\n",
        ),
        (
            "Started at: SANITIZED_TIME_OLD\nERROR: Old run error.\n",
            "sTaRtEd\tAt : SANITIZED_TIME_NEW\n"
            "WARNING: Diagnostic mentions Started at: NOT_ANCHOR\n",
        ),
        (
            "",
            "\n  WARNING: Synthetic warning.  \n\nERROR: Synthetic error.\n",
        ),
    ],
    ids=[
        "newest-product",
        "product-spaces",
        "product-tabs",
        "product-tight",
        "started-fallback",
        "started-ignores-midline",
        "no-anchor",
    ],
)
def test_parse_xstream_log_selects_newest_run_with_exact_text(
    previous_run: str,
    current_run: str,
) -> None:
    result = parse_xstream_log(previous_run + current_run)

    assert result.current_run_text == current_run
    lines = [line.strip() for line in current_run.splitlines()]
    assert result.warnings == tuple(
        line for line in lines if line.startswith("WARNING")
    )
    assert result.errors == tuple(line for line in lines if line.startswith("ERROR"))


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


def test_parse_xstream_log_empty_text_and_non_string_input() -> None:
    assert parse_xstream_log("") == XStreamLogResult(
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
    with pytest.raises(TypeError):
        parse_xstream_log(None)  # type: ignore[arg-type]
