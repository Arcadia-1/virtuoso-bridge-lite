"""Cross-reference and electrical checks for Circuit IR."""

from __future__ import annotations

from .ir import CircuitIr


class ValidationError(ValueError):
    """Raised when structurally valid IR has inconsistent design semantics."""


def validate_circuit_ir(ir: CircuitIr) -> None:
    instance_ids = {item.id for item in ir.instances}
    parameter_ids = {item.id for item in ir.parameters}
    group_ids = {item.id for item in ir.matching_groups}
    connected_nets = {net for item in ir.instances for net in item.terminals.values()}
    for port in ir.ports:
        if port.id not in connected_nets:
            raise ValidationError(f"critical port {port.id} is floating")
    for parameter in ir.parameters:
        for instance_id in parameter.linked_instances:
            if instance_id not in instance_ids:
                raise ValidationError(f"parameter {parameter.id} references unknown instance {instance_id}")
    for instance in ir.instances:
        for parameter_id in instance.optimization_refs:
            if parameter_id not in parameter_ids:
                raise ValidationError(f"instance {instance.id} references unknown parameter {parameter_id}")
        for group_id in instance.matching_groups:
            if group_id not in group_ids:
                raise ValidationError(f"instance {instance.id} references unknown matching group {group_id}")
    for group in ir.matching_groups:
        members = set(group.instances)
        for instance_id in members:
            if instance_id not in instance_ids:
                raise ValidationError(f"matching group {group.id} references unknown instance {instance_id}")
            instance = ir.instance(instance_id)
            if group.id not in instance.matching_groups:
                raise ValidationError(f"instance {instance_id} does not reference matching group {group.id}")
        for parameter_id in group.parameters:
            if parameter_id not in parameter_ids:
                raise ValidationError(f"matching group {group.id} references unknown parameter {parameter_id}")
            parameter = next(item for item in ir.parameters if item.id == parameter_id)
            if not members.issubset(set(parameter.linked_instances)):
                raise ValidationError(f"matching group {group.id} members are not all linked by parameter {parameter_id}")
