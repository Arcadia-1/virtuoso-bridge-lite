"""Cadence Maestro XML *filters* — strip raw ``maestro.sdb`` /
``active.state`` down to high-signal subsets per a YAML keep-list.

Deliberately does **not** parse XML into Python dicts.  The library's
position: XML files (raw + filtered) are the canonical setup format;
consumers that need structured data should read the XML themselves.

Pure functions — no I/O.  Both filters take / return XML strings.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml


_DEFAULT_FILTER_PATH = (
    Path(__file__).resolve().parents[1] / "snapshot_filter.yaml"
)


@lru_cache(maxsize=4)
def _load_filter_config(path: str | None = None) -> dict:
    """Read ``snapshot_filter.yaml`` (default location bundled in the
    package) and return ``{"maestro_sdb": {...}, "active_state": {...}}``.

    Cached: a snapshot pulls the same config many times.  Callers that
    need a custom location can pass ``path`` — pass an absolute string
    so the cache key is stable across calls.
    """
    p = Path(path) if path else _DEFAULT_FILTER_PATH
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _keep_set(section: str, key: str, fallback: Iterable[str]) -> frozenset[str]:
    """Pull a keep-list out of the YAML config, falling back to the hard-
    coded list when the file is unreadable / missing the entry."""
    cfg = _load_filter_config()
    raw = (cfg.get(section) or {}).get(key)
    if isinstance(raw, list) and raw:
        return frozenset(str(x) for x in raw if x)
    return frozenset(fallback)


# Hard-coded fallbacks — used only when the YAML file is unreadable.
# The YAML is the source of truth; these mirror its defaults so the
# filter still works even on a broken install.
_DEFAULT_SDB_ACTIVE_KEEP = (
    "currentmode", "jobcontrolmode", "corners", "tests", "vars",
    "parameters", "specs", "parametersets", "overwritehistoryname",
)
_DEFAULT_STATE_COMPONENT_KEEP = (
    "adeInfo", "analyses", "variables", "rfstim",
    "turboOptions",                              # Spectre X / APS / AX / CX
    "mdlOptions", "mtsSetup", "graphicalStimuli", # populated only when used
    # outputs / environmentOptions / modelSetup deliberately dropped —
    # redundant with SKILL track (see snapshot_filter.yaml notes).
)


def filter_sdb_xml(xml_text: str) -> str:
    """Return a stripped-down ``maestro.sdb`` XML — only the high-signal
    children of ``<active>`` survive; ``<history>`` and GUI prefs are
    dropped.

    Profile of a typical sdb:
      ~90% bytes in ``<history>`` (one full ``<active>`` snapshot per
      past run — historical noise);
      ~5% in GUI prefs (``<plottingoptions>``, ``<outputscustomcols>``,
      ``<runoptions>``, ``<extensions>``, ``<exploreroptions>``,
      ``<checksasserts>``, ``<incrementalsim*>``);
      ~5% the actual current setup (corners / tests / vars / parameters).

    The filter keeps only that last 5%.  Real cell tested:
    106 KB → ~10 KB (≥10× reduction) with no loss of "what's currently
    set up" information.

    The whitelist is loaded from ``resources/snapshot_filter.yaml`` —
    edit that file to change which ``<active>`` children survive.

    Pure function: takes / returns XML strings, no I/O.  Returns ``""``
    on parse error.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""

    keep = _keep_set("maestro_sdb", "active_keep", _DEFAULT_SDB_ACTIVE_KEEP)

    new_root = ET.Element("setupdb")
    for active in root.findall("active"):
        new_active = ET.SubElement(new_root, "active")
        for child in active:
            if child.tag in keep:
                new_active.append(child)

    ET.indent(new_root, space="  ")
    return ET.tostring(new_root, encoding="unicode")


def _sdb_active_tests(sdb_xml: str) -> set[str]:
    """Extract the set of test names declared under ``<active><tests>``
    in a ``maestro.sdb`` payload.  Used to drop stale ``<Test>`` blocks
    in ``active.state`` (Cadence keeps tombstones for removed tests).
    Returns empty set on parse error.
    """
    try:
        root = ET.fromstring(sdb_xml)
    except ET.ParseError:
        return set()
    out: set[str] = set()
    for active in root.findall("active"):
        tests_elem = active.find("tests")
        if tests_elem is None:
            continue
        for t in tests_elem.findall("test"):
            name = (t.text or "").strip()
            if name:
                out.add(name)
    return out


def filter_active_state_xml(xml_text: str, *,
                             valid_test_names: set[str] | None = None) -> str:
    """Return a stripped-down ``active.state`` XML — only the components
    flagged as high-signal in ``snapshot_filter.yaml`` are kept.

    ``active.state`` is per-test simulation setup.  Each ``<Test>`` block
    holds ~22 ``<component>`` children, most empty placeholders.  The
    important ones (analyses, variables, outputs, modelSetup, ...) carry
    the actual configuration; the others are GUI scratch.

    Cadence persists ``<Test>`` blocks for *every test ever configured*
    in this maestro view, even after the user removes a test from the
    GUI (sdb's ``<active><tests>`` updates immediately, but
    ``active.state`` keeps a tombstone).  Pass ``valid_test_names`` —
    typically the set returned by :func:`_sdb_active_tests` on the
    sibling ``maestro.sdb`` — to drop those tombstones.  When omitted,
    every ``<Test>`` block survives.

    The component whitelist is loaded from
    ``resources/snapshot_filter.yaml`` (key
    ``active_state.components_keep``).

    Pure function: takes / returns XML strings, no I/O.  Returns ``""``
    on parse error.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""

    keep = _keep_set("active_state", "components_keep",
                     _DEFAULT_STATE_COMPONENT_KEEP)

    new_root = ET.Element("statedb", root.attrib)
    for test in root.findall("Test"):
        if valid_test_names is not None and test.get("Name") not in valid_test_names:
            continue
        new_test = ET.SubElement(new_root, "Test", test.attrib)
        for comp in test.findall("component"):
            if comp.get("Name") in keep:
                new_test.append(comp)

    ET.indent(new_root, space="  ")
    return ET.tostring(new_root, encoding="unicode")
