# Two-Stage Miller Golden Topology

Version 1 supports an explicitly selected NMOS-input or PMOS-input two-stage
Miller op amp. The plan records a differential pair, matched active load, tail
bias, second stage, second-stage bias, Miller capacitor, and a disabled nulling
resistor slot.

The topology plugin describes roles, abstract device classes, terminals, nets,
matching groups, selection rationale, and known limits. It does not contain real
SMIC180 library/cell/view or CDF values. Those are supplied only by a confirmed
technology profile.

Ordinary open-loop AC results may report gain and unity-gain bandwidth. Phase
margin remains unverified until a dedicated, validated STB testbench exists.
