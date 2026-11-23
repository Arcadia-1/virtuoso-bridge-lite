"""Read Maestro configuration, environment, and simulation results.

Submodules (all internal except where re-exported below):

- ``_skill``        — low-level SKILL execution helpers
- ``_parse_skill``  — slot tokenizer + s-expr decoder for read_results
- ``_parse_sdb``    — XML *filters* for ``maestro.sdb`` / ``active.state``
- ``_compact``      — snapshot reshape helpers
- ``bundle``        — single-round-trip SKILL bundles for snapshot
- ``session``       — focused-session window probes
- ``runs``          — read_results / export_waveform
- ``snapshot``      — ``snapshot()`` aggregator (single entry — pass
                      ``output_root`` to also write the disk dump)

Library no longer ships any SKILL alist→dict or sdb XML→dict
parsers.  XML files (raw + filtered) and ``state_from_skill.txt``
(raw SKILL outputs, sectioned) are the canonical setup format.
"""

from ._parse_sdb import filter_active_state_xml, filter_sdb_xml
from .runs import NoResults, check_points, export_waveform, read_results
from .snapshot import snapshot
from .state import (
    MaestroSessionState,
    MaestroStateProbeError,
    get_session_state,
    list_session_states,
)


__all__ = [
    "filter_sdb_xml",
    "filter_active_state_xml",
    "read_results",
    "NoResults",
    "check_points",
    "export_waveform",
    "snapshot",
    "MaestroSessionState",
    "MaestroStateProbeError",
    "get_session_state",
    "list_session_states",
]
