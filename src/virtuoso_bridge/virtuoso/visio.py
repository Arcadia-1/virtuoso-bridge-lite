"""Export schematic data to Microsoft Visio.

Adapted from the `cadence_to_visio` project:
https://github.com/Ruadaa/cadence_to_visio/

The conversion is split in two layers:

* ``build_visio_schematic`` converts ``read_schematic()`` output into a
  deterministic, testable model with instance boxes, pin coordinates, and net
  segments.
* ``export_model_to_visio`` is the optional Windows/pywin32 layer that draws
  that model in Visio.

Known follow-ups:

* RC/source placement follows the original `cadence_to_visio` stencil-relative
  pin model. For tighter alignment, export actual symbol terminal centers from
  Virtuoso instead of tuning per-device offsets by hand.
* A future non-Windows backend could use a pure-Python `.vsdx` writer such as
  `dave-howard/vsdx`, but that would need separate symbol/template handling
  because it does not drive Visio's `.vss` stencil masters through COM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

Point = tuple[float, float]
Segment = tuple[Point, Point]


def default_stencil_path() -> Path | None:
    """Return the first available `circuit.vss` stencil path."""
    repo_root = Path(__file__).resolve().parents[3]
    candidates = (
        Path.cwd() / "circuit.vss",
        Path.cwd() / "examples" / "circuit.vss",
        repo_root / "examples" / "circuit.vss",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


@dataclass(frozen=True)
class PinSpec:
    """Relative pin location on a Visio master.

    ``rel_x`` and ``rel_y`` are normalized to the instance size, so ``0.5`` is
    the right/top edge and ``-0.5`` is the left/bottom edge.
    """

    name: str
    rel_x: float
    rel_y: float


@dataclass(frozen=True)
class DeviceSpec:
    """Mapping from Virtuoso instance/cell names to a Visio master."""

    device_type: str
    master_name: str
    size: tuple[float, float]
    pins: tuple[PinSpec, ...]
    name_prefixes: tuple[str, ...] = ()
    cell_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisioPin:
    """A placed terminal in Visio page coordinates."""

    instance: str
    name: str
    net: str
    x: float
    y: float


@dataclass(frozen=True)
class VisioInstance:
    """A schematic instance ready for Visio placement."""

    name: str
    lib: str
    cell: str
    device_type: str
    master_name: str
    x: float
    y: float
    width: float
    height: float
    orient: str = "R0"
    pins: dict[str, VisioPin] = field(default_factory=dict)


@dataclass(frozen=True)
class VisioNet:
    """A net and the line segments used to draw it."""

    name: str
    pins: tuple[VisioPin, ...]
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class VisioSchematic:
    """A complete Visio export model."""

    instances: tuple[VisioInstance, ...]
    nets: Mapping[str, VisioNet]


UNKNOWN_DEVICE = DeviceSpec(
    "UNKNOWN",
    "Unknown",
    (0.43, 0.43),
    (
        PinSpec("P1", 0.0, 0.5),
        PinSpec("P2", 0.0, -0.5),
        PinSpec("P3", 0.5, 0.0),
        PinSpec("P4", -0.5, 0.0),
        PinSpec("P5", 0.0, 0.0),
    ),
)

DEFAULT_DEVICE_LIBRARY: tuple[DeviceSpec, ...] = (
    DeviceSpec(
        "NMOS",
        "NMOS",
        (0.44, 0.59),
        (
            PinSpec("D", 0.5, 0.5),
            PinSpec("G", -0.5, 0.0017),
            PinSpec("S", 0.5, -0.5),
            PinSpec("B", 0.4759, 0.0),
        ),
        name_prefixes=("NM", "MN", "M"),
        cell_keywords=("nmos", "nch"),
    ),
    DeviceSpec(
        "PMOS",
        "PMOS",
        (0.44, 0.59),
        (
            PinSpec("D", 0.5, -0.5),
            PinSpec("G", -0.5, 0.0017),
            PinSpec("S", 0.5, 0.5),
            PinSpec("B", 0.4759, 0.0),
        ),
        name_prefixes=("PM", "MP"),
        cell_keywords=("pmos", "pch"),
    ),
    DeviceSpec(
        "RES",
        "R",
        (0.20, 0.59),
        (
            PinSpec("PLUS", 0.0, 0.5),
            PinSpec("MINUS", 0.0, -0.5),
            PinSpec("P", 0.0, 0.5),
            PinSpec("N", 0.0, -0.5),
            PinSpec("R_up", 0.0, 0.5),
            PinSpec("R_down", 0.0, -0.5),
        ),
        name_prefixes=("R",),
        cell_keywords=("res", "rpoly", "rppoly"),
    ),
    DeviceSpec(
        "CAP",
        "C",
        (0.20, 0.59),
        (
            PinSpec("PLUS", 0.0, 0.5),
            PinSpec("MINUS", 0.0, -0.5),
            PinSpec("P", 0.0, 0.5),
            PinSpec("N", 0.0, -0.5),
            PinSpec("C_up", 0.0, 0.5),
            PinSpec("C_down", 0.0, -0.5),
        ),
        name_prefixes=("C",),
        cell_keywords=("cap", "mom", "mim"),
    ),
    DeviceSpec(
        "IND",
        "L",
        (0.20, 0.59),
        (
            PinSpec("PLUS", 0.0, 0.5),
            PinSpec("MINUS", 0.0, -0.5),
            PinSpec("P", 0.0, 0.5),
            PinSpec("N", 0.0, -0.5),
        ),
        name_prefixes=("L",),
        cell_keywords=("ind", "rind", "spiral"),
    ),
    DeviceSpec(
        "VSRC",
        "DC-V",
        (0.43, 0.43),
        (
            PinSpec("PLUS", 0.0, 0.5),
            PinSpec("MINUS", 0.0, -0.5),
            PinSpec("P", 0.0, 0.5),
            PinSpec("N", 0.0, -0.5),
        ),
        name_prefixes=("V",),
        cell_keywords=("vdc", "vpulse", "vsin", "vpwl", "vac", "vsource"),
    ),
    DeviceSpec(
        "ISRC",
        "DC-I",
        (0.43, 0.43),
        (
            PinSpec("PLUS", 0.0, 0.5),
            PinSpec("MINUS", 0.0, -0.5),
            PinSpec("P", 0.0, 0.5),
            PinSpec("N", 0.0, -0.5),
        ),
        name_prefixes=("I",),
        cell_keywords=("idc", "ipulse", "isin", "ipwl", "iac", "isource"),
    ),
)


def classify_instance(
    inst: Mapping[str, Any],
    library: Sequence[DeviceSpec] = DEFAULT_DEVICE_LIBRARY,
) -> DeviceSpec:
    """Return the best Visio device spec for a schematic instance."""

    cell = str(inst.get("cell", "")).lower()
    name = str(inst.get("name", "")).upper()

    for spec in library:
        if any(keyword in cell for keyword in spec.cell_keywords):
            return spec

    candidates: list[tuple[int, DeviceSpec]] = []
    for spec in library:
        for prefix in spec.name_prefixes:
            if name.startswith(prefix.upper()):
                candidates.append((len(prefix), spec))
    if candidates:
        return sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]
    return UNKNOWN_DEVICE


def build_visio_schematic(
    schematic: Mapping[str, Any],
    *,
    scale: float = 1.0,
    library: Sequence[DeviceSpec] = DEFAULT_DEVICE_LIBRARY,
    exclude_nets: Iterable[str] = (),
    exclude_pins: Iterable[str] = ("B",),
    include_single_pin_nets: bool = False,
) -> VisioSchematic:
    """Build a Visio export model from ``read_schematic()`` data."""

    excluded_nets = {n.upper() for n in exclude_nets}
    excluded_pins = {p.upper() for p in exclude_pins}
    instances: list[VisioInstance] = []
    net_to_pins: dict[str, list[VisioPin]] = {}

    for raw_inst in schematic.get("instances", []):
        spec = classify_instance(raw_inst, library)
        x, y = _scaled_xy(raw_inst.get("xy", [0.0, 0.0]), scale)
        width, height = spec.size
        orient = str(raw_inst.get("orient", "R0") or "R0")
        terms = dict(raw_inst.get("terms", {}))
        pin_map: dict[str, VisioPin] = {}

        for index, (term_name, net_name) in enumerate(terms.items()):
            term = str(term_name)
            net = str(net_name)
            if term.upper() in excluded_pins or net.upper() in excluded_nets:
                continue
            rel_x, rel_y = _pin_relative_position(spec, term, index)
            dx, dy = _transform_offset(rel_x * width, rel_y * height, orient)
            pin = VisioPin(
                instance=str(raw_inst.get("name", "")),
                name=term,
                net=net,
                x=x + dx,
                y=y + dy,
            )
            pin_map[term] = pin
            net_to_pins.setdefault(net, []).append(pin)

        instances.append(
            VisioInstance(
                name=str(raw_inst.get("name", "")),
                lib=str(raw_inst.get("lib", "")),
                cell=str(raw_inst.get("cell", "")),
                device_type=spec.device_type,
                master_name=spec.master_name,
                x=x,
                y=y,
                width=width,
                height=height,
                orient=orient,
                pins=pin_map,
            )
        )

    nets: dict[str, VisioNet] = {}
    for net_name, pins in sorted(net_to_pins.items()):
        if len(pins) < 2 and not include_single_pin_nets:
            continue
        points = [(pin.x, pin.y) for pin in pins]
        nets[net_name] = VisioNet(
            name=net_name,
            pins=tuple(pins),
            segments=tuple(minimum_spanning_segments(points)),
        )

    return VisioSchematic(instances=tuple(instances), nets=nets)


def export_schematic_to_visio(
    client: Any,
    lib: str | None = None,
    cell: str | None = None,
    *,
    output_path: str | Path | None = None,
    stencil_path: str | Path | None = None,
    visible: bool = True,
    scale: float = 1.0,
    exclude_nets: Iterable[str] = (),
    exclude_pins: Iterable[str] = ("B",),
) -> VisioSchematic:
    """Read a Virtuoso schematic through the bridge and draw it in Visio."""

    from virtuoso_bridge.virtuoso.schematic.reader import read_schematic

    data = read_schematic(client, lib, cell, include_positions=True)
    model = build_visio_schematic(
        data,
        scale=scale,
        exclude_nets=exclude_nets,
        exclude_pins=exclude_pins,
    )
    export_model_to_visio(
        model,
        output_path=output_path,
        stencil_path=stencil_path,
        visible=visible,
    )
    return model


def export_model_to_visio(
    model: VisioSchematic,
    *,
    output_path: str | Path | None = None,
    stencil_path: str | Path | None = None,
    visible: bool = True,
) -> None:
    """Draw a Visio model using Microsoft Visio COM automation.

    This function requires Windows, Microsoft Visio, ``pywin32``, and the
    ``circuit.vss`` stencil copied from ``cadence_to_visio``. The pure model
    builder above remains importable and testable without those dependencies.
    """

    try:
        import win32com.client
    except ImportError as exc:
        raise RuntimeError(
            "Visio export requires pywin32 on Windows: "
            "pip install pywin32"
        ) from exc

    resolved_stencil = Path(stencil_path) if stencil_path else default_stencil_path()
    if resolved_stencil is None or not resolved_stencil.exists():
        raise RuntimeError(
            "Visio export requires circuit.vss. Put circuit.vss in the current "
            "directory, or pass --stencil /path/to/circuit.vss."
        )

    visio = win32com.client.Dispatch("Visio.Application")
    visio.Visible = bool(visible)
    visio.Documents.Add("")
    page = visio.ActivePage

    masters = {}
    stencil = visio.Documents.OpenEx(str(resolved_stencil), 64)
    wanted = {inst.master_name for inst in model.instances}
    for master_name in wanted:
        try:
            masters[master_name] = stencil.Masters(master_name)
        except Exception:
            pass
    missing = sorted(wanted - set(masters))
    if missing:
        raise RuntimeError(
            "Visio stencil is missing required masters: " + ", ".join(missing)
        )

    for inst in model.instances:
        master = masters.get(inst.master_name)
        shape = page.Drop(master, inst.x, inst.y)
        shape.Text = inst.name
        shape.CellsU("Width").ResultIU = inst.width
        shape.CellsU("Height").ResultIU = inst.height
        _apply_visio_orientation(shape, inst.orient)

    for net in model.nets.values():
        for segment in net.segments:
            for (x1, y1), (x2, y2) in _orthogonal_segments(segment):
                line = page.DrawLine(x1, y1, x2, y2)
                _solidify_line(line)

    _solidify_all_lines(page)
    if output_path:
        visio.ActiveDocument.SaveAs(str(Path(output_path)))


def minimum_spanning_segments(points: Sequence[Point]) -> list[Segment]:
    """Return Manhattan-distance MST segments for a set of points."""

    if len(points) < 2:
        return []

    edges = []
    for i, p1 in enumerate(points):
        for j, p2 in enumerate(points):
            if i < j:
                distance = abs(p1[0] - p2[0]) + abs(p1[1] - p2[1])
                edges.append((distance, i, j))
    edges.sort()

    parent = list(range(len(points)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    segments: list[Segment] = []
    for _, i, j in edges:
        root_i = find(i)
        root_j = find(j)
        if root_i != root_j:
            parent[root_i] = root_j
            segments.append((points[i], points[j]))
    return segments


def _scaled_xy(value: Any, scale: float) -> Point:
    if isinstance(value, str):
        text = value.strip().strip("()")
        parts = text.split()
    else:
        parts = list(value or [])
    if len(parts) != 2:
        return (0.0, 0.0)
    return (float(parts[0]) * scale, float(parts[1]) * scale)


def _pin_relative_position(
    spec: DeviceSpec,
    term_name: str,
    index: int,
) -> Point:
    for pin in spec.pins:
        if pin.name.upper() == term_name.upper():
            return (pin.rel_x, pin.rel_y)
    if spec.device_type in {"RES", "CAP", "IND", "VSRC", "ISRC"}:
        return (0.0, 0.5 if index == 0 else -0.5)
    fallback = UNKNOWN_DEVICE.pins[index % len(UNKNOWN_DEVICE.pins)]
    return (fallback.rel_x, fallback.rel_y)


def _transform_offset(dx: float, dy: float, orient: str) -> Point:
    orient = (orient or "R0").upper()
    if orient == "R90":
        return (-dy, dx)
    if orient == "R180":
        return (-dx, -dy)
    if orient == "R270":
        return (dy, -dx)
    if orient == "MX":
        return (dx, -dy)
    if orient == "MY":
        return (-dx, dy)
    if orient == "MXR90":
        return (dy, dx)
    if orient == "MYR90":
        return (-dy, -dx)
    return (dx, dy)


def _apply_visio_orientation(shape: Any, orient: str) -> None:
    import math

    angle_map = {
        "R0": 0,
        "R90": math.pi / 2,
        "R180": math.pi,
        "R270": 3 * math.pi / 2,
    }
    orient = (orient or "R0").upper()
    if orient in angle_map:
        shape.CellsU("Angle").ResultIU = angle_map[orient]
    elif orient == "MX":
        shape.CellsU("FlipY").FormulaU = "1"
    elif orient == "MY":
        shape.CellsU("FlipX").FormulaU = "1"
    elif orient == "MXR90":
        shape.CellsU("FlipY").FormulaU = "1"
        shape.CellsU("Angle").ResultIU = math.pi / 2
    elif orient == "MYR90":
        shape.CellsU("FlipX").FormulaU = "1"
        shape.CellsU("Angle").ResultIU = math.pi / 2


def _orthogonal_segments(segment: Segment) -> list[Segment]:
    (x1, y1), (x2, y2) = segment
    if abs(x1 - x2) < 1e-9 or abs(y1 - y2) < 1e-9:
        return [segment]
    corner = (x2, y1)
    return [((x1, y1), corner), (corner, (x2, y2))]


def _solidify_line(line: Any) -> None:
    line.CellsU("ConFixedCode").FormulaU = "3"
    line.CellsU("LineWeight").FormulaU = "1.2 pt"
    line.CellsU("LinePattern").FormulaU = "1"
    line.CellsU("RouteStyle").FormulaU = "16"


def _solidify_all_lines(page: Any) -> None:
    """Make generated connector/line shapes final-looking before save."""
    for shape in page.Shapes:
        try:
            if shape.OneD and shape.CellExistsU("LinePattern", 0):
                _solidify_line(shape)
        except Exception:
            pass
