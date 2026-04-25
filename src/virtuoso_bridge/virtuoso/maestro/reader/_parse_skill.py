"""Light SKILL output tokenizer used by ``bundle.py`` to slot the
single big SKILL response into per-call segments, plus a small
s-expression decoder for ``read_results`` (which returns numeric
post-simulation values that callers actively compute on).

Pure functions — no I/O.  Deliberately *not* a full SKILL alist→dict
parser for setup data: snapshot output keeps SKILL setup as raw text
in ``state_from_skill.txt``; consumers read it directly.
"""

from __future__ import annotations

import re


def _parse_skill_str_list(raw: str) -> list[str]:
    """Parse a flat SKILL list of strings like ``("a" "b" "c")`` →
    ``['a', 'b', 'c']``.  Returns ``[]`` on empty / ``nil`` / no quotes."""
    if not raw:
        return []
    s = raw.strip()
    if s in ("", "nil"):
        return []
    return re.findall(r'"([^"]*)"', s)


def _tokenize_top_level(body: str, *,
                         include_groups: bool = True,
                         include_strings: bool = False,
                         include_atoms: bool = False,
                         max_tokens: int | None = None) -> list[str]:
    """Split ``body`` into top-level SKILL tokens, respecting quotes/parens.

    A token is one of:
      - a balanced ``(...)`` group (always)
      - a double-quoted ``"..."`` string (yielded if ``include_strings``)
      - a bare atom — a whitespace-delimited run containing no ``(``/``)`` —
        (yielded if ``include_atoms``)

    Quote and paren balancing is tracked across the whole scan; neither
    escapes nor nested quotes fool us.  Stops early once ``max_tokens``
    have been emitted.
    """
    tokens: list[str] = []
    i, n = 0, len(body)
    while i < n and (max_tokens is None or len(tokens) < max_tokens):
        ch = body[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and not (body[j] == '"' and body[j - 1] != "\\"):
                j += 1
            tok = body[i:j + 1]
            if include_strings:
                tokens.append(tok)
            i = j + 1
            continue
        if ch == "(":
            depth = 1
            j = i + 1
            in_str = False
            while j < n and depth:
                c = body[j]
                if in_str:
                    if c == '"' and body[j - 1] != "\\":
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            tok = body[i:j]
            if include_groups:
                tokens.append(tok)
            i = j
            continue
        # Bare atom: run until whitespace or paren.
        j = i
        while j < n and not body[j].isspace() and body[j] not in "()":
            j += 1
        if include_atoms:
            tokens.append(body[i:j])
        i = j
    return tokens


def _scan_top_groups(body: str) -> list[str]:
    """Split at top-level parens: ``(..) (..) (..)`` → list of ``(..)`` strings."""
    return _tokenize_top_level(body, include_groups=True,
                                include_strings=False, include_atoms=False)


def _parse_sexpr(tok: str):
    """Parse one SKILL atom or list into Python.

    ``"x"`` → ``"x"`` (unescaped), ``nil`` → ``None``, ``t`` → ``True``,
    ``(a b c)`` → list[...], bare number/symbol → original string.

    Used by :func:`runs.read_results` to decode post-simulation result
    values into numeric/string atoms that callers compute on.
    """
    tok = (tok or "").strip()
    if not tok:
        return None
    if tok == "nil":
        return None
    if tok == "t":
        return True
    if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if tok.startswith("(") and tok.endswith(")"):
        inner = tok[1:-1]
        items: list = []
        i = 0
        n = len(inner)
        while i < n:
            while i < n and inner[i].isspace():
                i += 1
            if i >= n:
                break
            if inner[i] == '"':
                j = i + 1
                while j < n and not (inner[j] == '"' and inner[j - 1] != "\\"):
                    j += 1
                items.append(_parse_sexpr(inner[i:j + 1]))
                i = j + 1
            elif inner[i] == "(":
                depth = 1
                j = i + 1
                while j < n and depth:
                    if inner[j] == "(":
                        depth += 1
                    elif inner[j] == ")":
                        depth -= 1
                    j += 1
                items.append(_parse_sexpr(inner[i:j]))
                i = j
            else:
                j = i
                while j < n and not inner[j].isspace() and inner[j] not in "()":
                    j += 1
                items.append(_parse_sexpr(inner[i:j]))
                i = j
        return items
    return tok
