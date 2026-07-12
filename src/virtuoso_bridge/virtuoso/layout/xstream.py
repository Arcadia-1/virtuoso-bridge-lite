"""Pure helpers for requesting and inspecting XStream GDS export."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

from virtuoso_bridge.virtuoso.ops import escape_skill_string
from virtuoso_bridge.virtuoso.skill_output import (
    is_single_complete_skill_list,
    parse_sexpr,
    tokenize_top_level,
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
_COMPLETION_MESSAGE_FIELD = "showCompletionMsgBox"
_COMPLETION_MESSAGE_OLD_VARIABLE = "vbOldShowCompletionMsgBox"
_PRODUCT_ANCHOR_RE = re.compile(
    r"^Product[ \t]*:[ \t]*Virtuoso\(R\)[ \t]+XStream[ \t]+Out",
    re.MULTILINE,
)
_STARTED_ANCHOR_RE = re.compile(
    r"^[ \t]*Started[ \t]+at[ \t]*:",
    re.IGNORECASE | re.MULTILINE,
)
_WARNING_LINE_RE = re.compile(r"^(?:WARNING(?:\s|:|\()|\*WARNING\*)")
_ERROR_LINE_RE = re.compile(r"^(?:ERROR(?:\s|:|\()|\*ERROR\*)")
_TRANSLATED_STRUCTURE_RE = re.compile(
    r"\bTranslating\s+cellview\s+"
    r"(?P<library>[^/\s]+)/(?P<cell>[^/\s]+)/(?P<view>[^/\s]+)\s+"
    r"as\s+STRUCTURE\s+(?P<structure>\S+?)\.\s*$"
)
_COMPLETION_PREFIX_PATTERN = (
    r"(?:INFO(?:[ \t]*\([ \t]*XSTRM-234[ \t]*\))?|XSTRM-234)"
    r"[ \t]*:[ \t]*"
)
_COMPLETION_LINE_RE = re.compile(
    rf"^{_COMPLETION_PREFIX_PATTERN}"
    r"Translation completed\.(?:[ \t]+.*)?[ \t]*$"
)
_COMPLETION_COUNTS_RE = re.compile(
    rf"^{_COMPLETION_PREFIX_PATTERN}"
    r"Translation completed\.\s*"
    r"(?P<error_quote>['\"]?)(?P<errors>\d+)(?P=error_quote)\s+"
    r"error\(s\)\s+and\s+"
    r"(?P<warning_quote>['\"]?)(?P<warnings>\d+)(?P=warning_quote)\s+"
    r"warning\(s\)\s+found\.[ \t]*$"
)
_MAX_COMPLETION_COUNT_DIGITS = 18
_TERMINAL_MARKERS = (
    re.compile(
        r"(?<![A-Za-z0-9_-])XSTRM-273(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_])Translation[ \t]+failed(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_-])OPEN_FAILED(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class XStreamExportRequest:
    """Execution-host XStream parameters for one GDS export request."""

    library: str
    top_cell: str
    view: str
    stream_file: str
    layer_map: str
    log_file: str
    run_dir: str


@dataclass(frozen=True)
class XStreamTranslatedStructure:
    """One XStream cellview-to-GDS structure translation."""

    library: str
    cell: str
    view: str
    structure: str


@dataclass(frozen=True)
class XStreamLogResult:
    """Parsed status and diagnostics from the current XStream log run."""

    completed: bool
    completion_line: str | None
    error_count: int | None
    warning_count: int | None
    translated_structures: tuple[XStreamTranslatedStructure, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    terminal_failures: tuple[str, ...]
    parse_errors: tuple[str, ...]
    current_run_text: str


@dataclass(frozen=True)
class _XStreamRequestResponse:
    state: Literal["started", "failed"]
    body_error: str | None
    cleanup_failures: tuple[str, ...]


def xstream_export_gds_skill(request: XStreamExportRequest) -> str:
    """Render SKILL that submits an XStream export and restores its fields."""
    escaped_values: dict[str, str] = {}
    for request_field, _xstream_field, _old_variable in _XSTREAM_FIELDS:
        value = getattr(request, request_field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{request_field} must be a nonempty string")
        escaped_values[request_field] = escape_skill_string(value)

    old_variables = " ".join(
        (
            *(old_variable for _, _, old_variable in _XSTREAM_FIELDS),
            _COMPLETION_MESSAGE_OLD_VARIABLE,
        )
    )
    captures = "".join(
        f'{old_variable} = xstGetField("{xstream_field}") '
        for _request_field, xstream_field, old_variable in _XSTREAM_FIELDS
    )
    captures += (
        f'{_COMPLETION_MESSAGE_OLD_VARIABLE} = '
        f'xstGetField("{_COMPLETION_MESSAGE_FIELD}") '
    )
    setters = "".join(
        f'xstSetField("{xstream_field}" "{escaped_values[request_field]}") '
        for request_field, xstream_field, _old_variable in _XSTREAM_FIELDS
    )
    setters += f'xstSetField("{_COMPLETION_MESSAGE_FIELD}" "false") '
    restored_fields = tuple(
        (xstream_field, old_variable)
        for _request_field, xstream_field, old_variable in _XSTREAM_FIELDS
    ) + (
        (
            _COMPLETION_MESSAGE_FIELD,
            _COMPLETION_MESSAGE_OLD_VARIABLE,
        ),
    )
    restorations = "".join(
        (
            f'vbCleanup = errset(xstSetField("{xstream_field}" '
            f"{old_variable}) nil) "
            "unless(vbCleanup "
            f'vbCleanupFailures = cons("failed to restore XStream field '
            f'{xstream_field}" vbCleanupFailures)) '
        )
        for xstream_field, old_variable in restored_fields
    )

    return (
        f"let(({old_variables} vbCaptured vbBodyAttempt vbBodyError "
        "vbCleanup vbCleanupFailures) "
        'vbBodyError = "XStream export request failed" '
        "unwindProtect("
        "progn("
        "vbBodyAttempt = errset(progn("
        "unless(and(isCallable('xstGetField) isCallable('xstSetField) "
        "isCallable('xstOutDoTranslate)) "
        'error("XStream APIs unavailable")) '
        f"{captures}"
        "vbCaptured = t "
        f"{setters}"
        "xstOutDoTranslate()) nil) "
        'unless(vbBodyAttempt vbBodyError = sprintf(nil "%L" errset.errset)) '
        "vbBodyAttempt) "
        "progn("
        "when(vbCaptured "
        f"{restorations}"
        ")) "
        ") "
        "if(vbBodyAttempt "
        'then list("xstreamRequest" "started" nil reverse(vbCleanupFailures)) '
        'else list("xstreamRequest" "failed" '
        "vbBodyError reverse(vbCleanupFailures))))"
    )


def parse_xstream_log(text: str) -> XStreamLogResult:
    """Parse the newest XStream Out run from log text."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")

    current_run_text = _select_current_run_text(text)
    lines = tuple(
        line.strip() for line in current_run_text.splitlines() if line.strip()
    )

    warnings = tuple(line for line in lines if _WARNING_LINE_RE.match(line))
    errors = tuple(line for line in lines if _ERROR_LINE_RE.match(line))

    translated_structures: list[XStreamTranslatedStructure] = []
    terminal_failures: list[str] = []
    for line in lines:
        match = _TRANSLATED_STRUCTURE_RE.search(line)
        if match is not None:
            translated_structures.append(
                XStreamTranslatedStructure(
                    library=match.group("library"),
                    cell=match.group("cell"),
                    view=match.group("view"),
                    structure=match.group("structure"),
                )
            )
            continue
        if any(marker.search(line) for marker in _TERMINAL_MARKERS):
            terminal_failures.append(line)

    completion_lines = tuple(
        line for line in lines if _COMPLETION_LINE_RE.fullmatch(line)
    )
    completion_line = completion_lines[-1] if completion_lines else None
    error_count = None
    warning_count = None
    parse_errors: tuple[str, ...] = ()
    if completion_line is not None:
        malformed_counts = (
            f"malformed XStream completion counts: {completion_line}",
        )
        match = _COMPLETION_COUNTS_RE.fullmatch(completion_line)
        if match is None:
            parse_errors = malformed_counts
        else:
            error_digits = match.group("errors")
            warning_digits = match.group("warnings")
            if max(len(error_digits), len(warning_digits)) > (
                _MAX_COMPLETION_COUNT_DIGITS
            ):
                parse_errors = malformed_counts
            else:
                try:
                    error_count, warning_count = (
                        int(error_digits),
                        int(warning_digits),
                    )
                except ValueError:
                    parse_errors = malformed_counts

    return XStreamLogResult(
        completed=completion_line is not None,
        completion_line=completion_line,
        error_count=error_count,
        warning_count=warning_count,
        translated_structures=tuple(translated_structures),
        warnings=warnings,
        errors=errors,
        terminal_failures=tuple(terminal_failures),
        parse_errors=parse_errors,
        current_run_text=current_run_text,
    )


def _select_current_run_text(text: str) -> str:
    product_matches = tuple(_PRODUCT_ANCHOR_RE.finditer(text))
    if product_matches:
        return text[product_matches[-1].start() :]

    started_matches = tuple(_STARTED_ANCHOR_RE.finditer(text))
    if started_matches:
        return text[started_matches[-1].start() :]
    return text


def _parse_xstream_request_response(output: str) -> _XStreamRequestResponse:
    text = output.strip() if isinstance(output, str) else ""
    if not is_single_complete_skill_list(text):
        _raise_malformed_response(output)

    tokens = tokenize_top_level(
        text[1:-1],
        include_groups=True,
        include_strings=True,
        include_atoms=True,
    )
    if not _is_canonical_xstream_response_tokens(tokens):
        _raise_malformed_response(output)

    parsed = parse_sexpr(text)
    if not isinstance(parsed, list) or len(parsed) != 4:
        _raise_malformed_response(output)

    tag, state, body_error, cleanup_failures = parsed
    if (
        tag != "xstreamRequest"
        or not isinstance(state, str)
        or state not in {"started", "failed"}
    ):
        _raise_malformed_response(output)
    if body_error is not None and not isinstance(body_error, str):
        _raise_malformed_response(output)

    if cleanup_failures is None:
        cleanup = ()
    elif isinstance(cleanup_failures, list) and all(
        isinstance(item, str) for item in cleanup_failures
    ):
        cleanup = tuple(cleanup_failures)
    else:
        _raise_malformed_response(output)

    if state == "started":
        if body_error is not None:
            _raise_malformed_response(output)
    elif body_error is None:
        _raise_malformed_response(output)

    return _XStreamRequestResponse(
        state=cast(Literal["started", "failed"], state),
        body_error=body_error,
        cleanup_failures=cleanup,
    )


def _is_canonical_xstream_response_tokens(tokens: list[str]) -> bool:
    if len(tokens) != 4:
        return False
    tag, state, body_error, cleanup_failures = tokens
    if not _is_quoted_skill_string(tag) or not _is_quoted_skill_string(state):
        return False
    if body_error != "nil" and not _is_quoted_skill_string(body_error):
        return False
    if cleanup_failures == "nil":
        return True
    if not cleanup_failures.startswith("(") or not cleanup_failures.endswith(")"):
        return False
    cleanup_tokens = tokenize_top_level(
        cleanup_failures[1:-1],
        include_groups=True,
        include_strings=True,
        include_atoms=True,
    )
    return all(_is_quoted_skill_string(token) for token in cleanup_tokens)


def _is_quoted_skill_string(token: str) -> bool:
    return len(token) >= 2 and token.startswith('"') and token.endswith('"')


def _raise_malformed_response(output: object) -> None:
    raise ValueError(f"malformed XStream request response: {output!r}")


__all__ = [
    "XStreamExportRequest",
    "xstream_export_gds_skill",
    "XStreamTranslatedStructure",
    "XStreamLogResult",
    "parse_xstream_log",
]
