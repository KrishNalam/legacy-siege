"""Registry of predetermined, reviewed remediations the Upgrader may ship.

The Upgrader never invents code: an upgrade request is matched to one of these by target system and,
when the request names one, its fault class (advisory: if a system has a single remediation it is used
whatever the class is called). Each remediation module exposes NAME, SYSTEM, FUNCTION, FAULT_CLASSES,
PROVE_FAULT and the render/detect helpers upgrader.pipeline uses. Problems no code remediation covers
can still be shipped with a vetted toolsmith tool (upgrader.tools, remediation_kind "tool").
"""
from __future__ import annotations

from types import ModuleType

from . import batch_validation

VETTED: list[ModuleType] = [batch_validation]
REMEDIATIONS: dict[str, ModuleType] = {r.SYSTEM: r for r in reversed(VETTED)}  # first per system (compat)


def for_request(system: str, fault_class: str | None = None) -> ModuleType | None:
    """The remediation for a system, preferring one whose FAULT_CLASSES contains fault_class."""
    cands = [r for r in VETTED if r.SYSTEM == system]
    if fault_class:
        exact = [r for r in cands if fault_class in getattr(r, "FAULT_CLASSES", ())]
        if exact:
            return exact[0]
    return cands[0] if cands else None
