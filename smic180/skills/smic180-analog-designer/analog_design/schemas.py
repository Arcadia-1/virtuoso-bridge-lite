"""Versioned JSON Schema artifacts for the analog design contracts."""

from __future__ import annotations

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def design_spec_schema() -> dict:
    object_type = {"type": "object"}
    return {
        "$schema": _SCHEMA,
        "$id": "urn:smic180:analog-design:design-spec:v1",
        "title": "SMIC180 Analog Design Specification v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "version", "metadata", "technology", "circuit", "interfaces",
            "operating_conditions", "loads", "metrics", "pvt", "preferences", "publication",
        ],
        "properties": {
            "version": {"const": 1},
            "metadata": object_type,
            "technology": object_type,
            "circuit": object_type,
            "interfaces": object_type,
            "operating_conditions": object_type,
            "loads": object_type,
            "metrics": {"type": "array", "minItems": 1, "items": {"type": "object"}},
            "pvt": object_type,
            "preferences": object_type,
            "publication": object_type,
        },
    }


def circuit_ir_schema() -> dict:
    object_type = {"type": "object"}
    instance_required = [
        "id", "role", "device_class", "master_ref", "terminals",
        "logical_parameters", "physical_parameters", "cdf_expectations",
        "optimization_refs", "matching_groups", "rationale",
    ]
    return {
        "$schema": _SCHEMA,
        "$id": "urn:smic180:analog-design:circuit-ir:v1",
        "title": "SMIC180 Circuit IR v1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "version", "metadata", "technology", "circuit", "ports", "nets",
            "instances", "parameters", "matching_groups", "supplies", "biases",
            "analyses", "measurements", "constraints", "optimization", "provenance",
        ],
        "properties": {
            "version": {"const": 1},
            "metadata": object_type,
            "technology": object_type,
            "circuit": object_type,
            "ports": {"type": "array", "items": {"type": "object", "required": ["id", "direction", "kind"]}},
            "nets": {"type": "array", "items": {"type": "object", "required": ["id", "critical"]}},
            "instances": {"type": "array", "items": {"type": "object", "required": instance_required}},
            "parameters": {"type": "array", "items": {"type": "object", "required": ["id", "dimension", "value", "bounds", "target", "linked_instances", "provenance"]}},
            "matching_groups": {"type": "array", "items": {"type": "object", "required": ["id", "instances", "parameters"]}},
            "supplies": {"type": "array", "items": object_type},
            "biases": {"type": "array", "items": object_type},
            "analyses": {"type": "array", "items": object_type},
            "measurements": {"type": "array", "items": object_type},
            "constraints": {"type": "array", "items": object_type},
            "optimization": object_type,
            "provenance": object_type,
        },
    }