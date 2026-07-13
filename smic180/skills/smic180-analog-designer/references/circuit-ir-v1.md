# Circuit IR Version 1

Circuit IR is the authoritative design representation before Virtuoso handoff.
It contains explicit ports, nets, instances, parameters, matching intent,
analyses, measurements, constraints, optimization intent, and provenance.

Device instances use stable technology `master_ref` keys. Real Cadence masters,
terminal names, and CDF properties are resolved only through a confirmed
technology profile. Logical sizing, normalized physical candidates, reopened CDF
values, and published optimizer values remain separate records.

The loader enforces structural integrity. `validate_circuit_ir()` enforces
cross-references, critical connectivity, and matching semantics. Canonical JSON
serialization produces the immutable design digest.
