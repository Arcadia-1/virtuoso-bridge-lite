'''Verification profile data model for analog optimization V2.'''

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple


@dataclass(frozen=True)
class VerificationProfileConfig:
    '''One independently netlisted and simulated verification testbench.'''

    id: str
    role: str
    testbench_cell: str
    dut_instance: str
    stimuli: Mapping[str, Mapping[str, Any]]
    analyses: Tuple[Mapping[str, Any], ...]
    metrics: Tuple[Mapping[str, Any], ...]
    specs: Tuple[Mapping[str, Any], ...]
    pvt_policy: str = 'full'
    timeout_s: int = 1800
