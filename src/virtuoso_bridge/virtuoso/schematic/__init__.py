"""SKILL builders for Cadence Virtuoso schematic editing."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from virtuoso_bridge.virtuoso.schematic.editor import SchematicEditor
from virtuoso_bridge.virtuoso.schematic.ops import (
    schematic_check,
    schematic_create_net_expression,
    schematic_create_inst,
    schematic_create_inst_by_master_name,
    schematic_create_pin,
    schematic_create_pin_at_instance_term,
    schematic_create_wire_between_instance_terms,
    schematic_create_net_stub,
    schematic_label_instance_term,
    schematic_label_instance_term_offset,
    schematic_create_wire,
    schematic_create_wire_label,
    schematic_set_netset_property,
)
from virtuoso_bridge.virtuoso.schematic.netlist import (
    NetlistImportResult,
    SchematicNetlistExportResult,
    classify_netlist_import_log,
    export_schematic_netlist,
    import_netlist_schematic,
    parse_netlist_import_output,
    schematic_export_netlist_skill,
    schematic_import_netlist_skill,
)
from virtuoso_bridge.virtuoso.schematic.planner import (
    ConstraintDiagnostic,
    ConstraintLevel,
    ConstraintStrength,
    DeviceKind,
    DifferentialPairConstraint,
    GridPlacement,
    GridPositionConstraint,
    PlannedInstance,
    PlannedPin,
    ReadbackMismatch,
    SchematicInstanceSpec,
    SchematicPinSpec,
    SchematicPlan,
    SchematicPlanRequest,
    SchematicPlanner,
    SchematicPlannerConfig,
    SchematicPlanningError,
    SchematicReadbackReport,
    infer_device_kind,
)

if TYPE_CHECKING:
    from virtuoso_bridge import VirtuosoClient


class _UseReaderDefault:
    """Sentinel type that preserves the standalone reader's default filtering."""


_USE_READER_DEFAULT = _UseReaderDefault()


class SchematicOps:
    """Attached to VirtuosoClient as ``client.schematic``."""

    def __init__(self, owner: VirtuosoClient) -> None:
        self._owner = owner

    def create(
        self,
        lib: str,
        cell: str,
        view: str = "schematic",
        timeout: int = 60,
    ) -> SchematicEditor:
        """Create a schematic editor, replacing an existing view if present.

        This is intentionally destructive: Cadence mode ``"w"`` recreates
        the target cellview. Use :meth:`modify` for an existing schematic.
        """
        return SchematicEditor(self._owner, lib, cell, view=view, mode="w", timeout=timeout)

    def modify(
        self,
        lib: str,
        cell: str,
        view: str = "schematic",
        timeout: int = 60,
    ) -> SchematicEditor:
        """Open a schematic editor in append mode without clearing its view."""
        return SchematicEditor(self._owner, lib, cell, view=view, mode="a", timeout=timeout)

    def edit(
        self,
        lib: str,
        cell: str,
        view: str = "schematic",
        mode: Literal["a", "w"] = "a",
        timeout: int = 60,
    ) -> SchematicEditor:
        """Deprecated compatibility wrapper for :meth:`create` / :meth:`modify`.

        ``edit()`` now defaults to safe append mode. Choose ``create()`` or
        ``modify()`` explicitly so overwrite intent is visible at the call site.
        """
        warnings.warn(
            "client.schematic.edit() is deprecated; use create() or modify() explicitly.",
            DeprecationWarning,
            stacklevel=2,
        )
        return SchematicEditor(self._owner, lib, cell, view=view, mode=mode, timeout=timeout)

    def read(
        self,
        lib: str | None = None,
        cell: str | None = None,
        *,
        include_positions: bool = False,
        param_filters: str | Path | None | _UseReaderDefault = _USE_READER_DEFAULT,
        timeout: int = 300,
    ) -> dict[str, Any]:
        """Read schematic topology, pins, nets, parameters, and optional geometry.

        This is the client-bound form of the legacy :func:`read_schematic`
        function and preserves its default CDF parameter filtering. Passing
        ``param_filters=None`` returns all CDF parameters.
        """
        from virtuoso_bridge.virtuoso.schematic.reader import read_schematic

        kwargs: dict[str, Any] = {
            "include_positions": include_positions,
            "timeout": timeout,
        }
        if not isinstance(param_filters, _UseReaderDefault):
            kwargs["param_filters"] = param_filters
        return read_schematic(self._owner, lib, cell, **kwargs)

    def plan(
        self,
        request: SchematicPlanRequest,
        *,
        config: SchematicPlannerConfig | None = None,
    ) -> SchematicPlan:
        """Create a deterministic, constraint-checked schematic plan."""
        return SchematicPlanner(config).plan(request)

    def create_from_plan(
        self,
        lib: str,
        cell: str,
        plan: SchematicPlan,
        *,
        view: str = "schematic",
        timeout: int = 60,
    ) -> None:
        """Create a cellview from a plan and run the editor's check/save path."""
        with self.create(lib, cell, view=view, timeout=timeout) as editor:
            plan.apply(editor)

    def export_netlist(
        self,
        lib: str,
        cell: str,
        output_dir: str | Path,
        *,
        view: str = "schematic",
        simulator: str = "spectre",
        recreate_all: bool = True,
        timeout: int = 120,
    ) -> SchematicNetlistExportResult:
        """Export a schematic netlist package through Virtuoso's netlister.

        The returned dictionary includes the Virtuoso-side ``source_file`` and
        ``source_dir``, the local ``output_dir`` and ``input_file``, plus the raw
        SKILL and download results. Existing ``output_dir`` contents are kept
        until the new package has downloaded and ``input.scs`` is present.
        """
        return export_schematic_netlist(
            self._owner,
            lib,
            cell,
            output_dir,
            view=view,
            simulator=simulator,
            recreate_all=recreate_all,
            timeout=timeout,
        )

    def import_netlist(
        self,
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
        run_dir: str | Path | None = None,
        timeout: int = 300,
    ) -> Any:
        """Import a netlist package through ``spiceIn`` and convert to schematic."""
        from virtuoso_bridge.virtuoso.schematic import netlist as schematic_netlist_module

        return schematic_netlist_module.import_netlist_schematic(
            self._owner,
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
            timeout=timeout,
        )


__all__ = [
    "SchematicOps",
    "SchematicEditor",
    "schematic_create_inst",
    "schematic_create_inst_by_master_name",
    "schematic_create_wire",
    "schematic_create_wire_label",
    "schematic_create_net_expression",
    "schematic_set_netset_property",
    "schematic_label_instance_term",
    "schematic_label_instance_term_offset",
    "schematic_create_pin",
    "schematic_create_pin_at_instance_term",
    "schematic_create_wire_between_instance_terms",
    "schematic_create_net_stub",
    "schematic_check",
    "SchematicNetlistExportResult",
    "schematic_export_netlist_skill",
    "export_schematic_netlist",
    "schematic_import_netlist_skill",
    "import_netlist_schematic",
    "NetlistImportResult",
    "parse_netlist_import_output",
    "classify_netlist_import_log",
    "ConstraintDiagnostic",
    "ConstraintLevel",
    "ConstraintStrength",
    "DeviceKind",
    "DifferentialPairConstraint",
    "GridPlacement",
    "GridPositionConstraint",
    "PlannedInstance",
    "PlannedPin",
    "ReadbackMismatch",
    "SchematicInstanceSpec",
    "SchematicPinSpec",
    "SchematicPlan",
    "SchematicPlanRequest",
    "SchematicPlanner",
    "SchematicPlannerConfig",
    "SchematicPlanningError",
    "SchematicReadbackReport",
    "infer_device_kind",
]
