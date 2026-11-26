"""Layout read/parse utilities."""

from __future__ import annotations

import re
from typing import Any

from virtuoso_bridge.virtuoso.skill_output import (
    _is_single_complete_skill_string,
    parse_sexpr,
)


def _decode_skill_output(raw: str) -> str:
    text = (raw or "").strip()
    if _is_single_complete_skill_string(text):
        return parse_sexpr(text)
    return text


def _decode_skill_string(value: Any) -> Any:
    if isinstance(value, str) and _is_single_complete_skill_string(value):
        return parse_sexpr(value)
    return value


_SKILL_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _parse_skill_numbers(value: str) -> list[float]:
    return [float(token) for token in _SKILL_NUMBER_RE.findall(value or "")]


def _parse_skill_point(value: str) -> tuple[float, float] | None:
    numbers = _parse_skill_numbers(value)
    return (numbers[0], numbers[1]) if len(numbers) >= 2 else None


def _parse_skill_point_list(value: str) -> list[tuple[float, float]] | None:
    numbers = _parse_skill_numbers(value)
    if len(numbers) < 2 or len(numbers) % 2 != 0:
        return None
    return [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)]


def parse_layout_geometry_output(raw: str) -> list[dict[str, Any]]:
    """Parse the line-oriented geometry dump returned by ``layout_read_geometry``."""
    decoded = _decode_skill_output(raw)
    if decoded.lstrip().startswith("ERROR:"):
        raise ValueError(decoded.strip())
    objects: list[dict[str, Any]] = []
    for line in decoded.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if fields[0] == "groups":
            from virtuoso_bridge.virtuoso.layout.groups import parse_layout_groups_output

            if len(fields) != 2 or not fields[1].startswith("data="):
                raise ValueError("invalid geometry groups row")
            objects.extend(parse_layout_groups_output(fields[1][5:]))
            continue
        obj: dict[str, Any] = {"kind": fields[0]}
        for field in fields[1:]:
            if "=" not in field:
                continue
            key, value = field.split("=", 1)
            obj[key] = None if value == "nil" else value
        if "bbox" in obj and isinstance(obj["bbox"], str):
            points = _parse_skill_point_list(obj["bbox"])
            obj["bbox"] = points if points and len(points) == 2 else obj["bbox"]
        if "points" in obj and isinstance(obj["points"], str):
            obj["points"] = _parse_skill_point_list(obj["points"])
        if "xy" in obj and isinstance(obj["xy"], str):
            obj["xy"] = _parse_skill_point(obj["xy"])
        for key in ("orient", "text"):
            if key in obj:
                obj[key] = _decode_skill_string(obj[key])
        if obj["kind"] == "mosaic":
            for key in ("rows", "columns"):
                value = obj.get(key)
                if isinstance(value, str) and re.fullmatch(r"\d+", value):
                    obj[key] = int(value)
            for key in ("lib", "cell", "view"):
                if key in obj:
                    obj[key] = _decode_skill_string(obj[key])
        objects.append(obj)
    return objects
