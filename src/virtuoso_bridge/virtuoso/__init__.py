"""Virtuoso tool family: Cadence Virtuoso control and editing primitives.

Subpackages:
  basic      – VirtuosoClient (SKILL execution over TCP)
  schematic  – SKILL builders for schematic editing
  layout     – SKILL builders for layout editing
  maestro    – ADE Assembler / Explorer setup + simulation reading

Top-level helpers:
  snapshot   – polymorphic snapshot of the focused window (dispatches
               to maestro / schematic / layout / ... based on what
               kind of Virtuoso window is focused)
"""

from .snapshot import classify_window, snapshot

__all__ = ["snapshot", "classify_window"]
