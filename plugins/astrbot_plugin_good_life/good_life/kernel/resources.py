"""Optional local executor hooks; not a host port or a new wire contract.

Existing start/resume still return a checkpoint. An executor can additionally
implement prepare_resources(instance, now, snapshot) -> ResourcePreparation and
abandon_resources(instance, result, now) -> tuple[Effect, ...]. Both are pure
calculations. Only registered domain writers execute effects in the kernel's
transaction. No callback may perform external work or open another transaction.
"""
from dataclasses import dataclass
from typing import Mapping

from ..contracts.models import Effect, RecordRef


@dataclass(frozen=True)
class ResourcePreparation:
    phase: str
    checkpoint: Mapping
    effects: tuple[Effect, ...] = ()
    reservation_refs: tuple[RecordRef, ...] = ()
