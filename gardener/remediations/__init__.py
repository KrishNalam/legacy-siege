"""Registry of predetermined, reviewed remediations the Gardener may ship.

The Gardener never invents code: an upgrade request is matched to one of these by
target system (and fault class when given). Scope cut (CONTRACTS): exactly one.
"""
from __future__ import annotations

from types import ModuleType

from . import batch_validation

REMEDIATIONS: dict[str, ModuleType] = {batch_validation.SYSTEM: batch_validation}


def for_request(system: str, fault_class: str | None = None) -> ModuleType | None:
    """The remediation for a system (fault_class is advisory: B may name classes differently)."""
    return REMEDIATIONS.get(system)
