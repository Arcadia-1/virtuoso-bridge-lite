"""Utilities for decoding SKILL values printed with ``%L``."""

from __future__ import annotations


def parse_skill_str_list(raw: str) -> list[str]:
    """Parse SKILL string values from a list or bare top-level strings."""
    text = (raw or "").strip()
    if not text or text == "nil":
        return []
    values: list[str] = []
    for token in tokenize_top_level(
        text,
        include_groups=True,
        include_strings=True,
        include_atoms=False,
    ):
        values.extend(_collect_strings(parse_sexpr(token)))
    return values


def tokenize_top_level(
    body: str,
    *,
    include_groups: bool = True,
    include_strings: bool = False,
    include_atoms: bool = False,
    max_tokens: int | None = None,
) -> list[str]:
    """Split ``body`` into top-level SKILL tokens, respecting strings/parens."""
    tokens: list[str] = []
    i, n = 0, len(body)
    while i < n and (max_tokens is None or len(tokens) < max_tokens):
        ch = body[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = _scan_string(body, i)
            if include_strings:
                tokens.append(body[i:j])
            i = j
            continue
        if ch == "(":
            j = _scan_group(body, i)
            if include_groups:
                tokens.append(body[i:j])
            i = j
            continue
        j = i
        while j < n and not body[j].isspace() and body[j] not in "()":
            j += 1
        if include_atoms:
            tokens.append(body[i:j])
        i = j
    return tokens


def scan_top_groups(body: str) -> list[str]:
    """Split top-level parenthesized groups: ``(..) (..)`` -> list of groups."""
    return tokenize_top_level(
        body,
        include_groups=True,
        include_strings=False,
        include_atoms=False,
    )


def parse_sexpr(tok: str):
    """Parse one SKILL atom or list into Python values.

    Strings are unescaped, ``nil`` maps to ``None``, ``t`` maps to ``True``,
    lists map recursively, and other atoms are returned as strings.
    """
    tok = (tok or "").strip()
    if not tok:
        return None
    if tok == "nil":
        return None
    if tok == "t":
        return True
    if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
        return _unescape_skill_string(tok[1:-1])
    if tok.startswith("(") and tok.endswith(")"):
        inner = tok[1:-1]
        return [
            parse_sexpr(token)
            for token in tokenize_top_level(
                inner,
                include_groups=True,
                include_strings=True,
                include_atoms=True,
            )
        ]
    return tok


def is_single_complete_skill_list(raw: str) -> bool:
    """Return whether text is exactly one balanced top-level SKILL list."""
    text = (raw or "").strip()
    if not text.startswith("("):
        return False

    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(text):
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
            if depth < 0 or (depth == 0 and index != len(text) - 1):
                return False
    return depth == 0 and not in_string


def _scan_string(text: str, start: int) -> int:
    i = start + 1
    while i < len(text):
        if text[i] == '"' and not _is_escaped(text, i):
            return i + 1
        i += 1
    return len(text)


def _scan_group(text: str, start: int) -> int:
    depth = 1
    i = start + 1
    in_str = False
    while i < len(text) and depth:
        ch = text[i]
        if in_str:
            if ch == '"' and not _is_escaped(text, i):
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return i


def _is_escaped(text: str, index: int) -> bool:
    slash_count = 0
    i = index - 1
    while i >= 0 and text[i] == "\\":
        slash_count += 1
        i -= 1
    return slash_count % 2 == 1


def _unescape_skill_string(value: str) -> str:
    chars: list[str] = []
    i = 0
    escapes = {
        "n": "\n",
        "t": "\t",
        "r": "\r",
        '"': '"',
        "\\": "\\",
    }
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            chars.append(escapes.get(nxt, "\\" + nxt))
            i += 2
            continue
        chars.append(ch)
        i += 1
    return "".join(chars)


def _collect_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(_collect_strings(item))
        return values
    return []
