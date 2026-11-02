"""Layout read/parse utilities."""

from __future__ import annotations

import re
from typing import Any

from virtuoso_bridge.virtuoso.skill_output import parse_sexpr


def _decode_skill_output(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return parse_sexpr(text)
    return text


def _parse_skill_numbers(value: str) -> list[float]:
    return [float(token) for token in re.findall(r"-?\d+(?:\.\d+)?", value or "")]


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
    objects: list[dict[str, Any]] = []
    for line in _decode_skill_output(raw).splitlines():
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
        if obj["kind"] == "mosaic":
            for key in ("rows", "columns"):
                value = obj.get(key)
                if isinstance(value, str) and re.fullmatch(r"\d+", value):
                    obj[key] = int(value)
            for key in ("lib", "cell", "view"):
                value = obj.get(key)
                if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
                    obj[key] = value[1:-1]
        objects.append(obj)
    return objects
