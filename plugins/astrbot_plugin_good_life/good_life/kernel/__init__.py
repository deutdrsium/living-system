"""A1 explicit opt-in API. No imports start timers, install schema or claim data."""
from .codec import KernelError
from .engine import ActivityKernel, KernelConfig, Preparation
from .gating import GateResult, GateSnapshot, KernelAccess, PreparedActivity
from .registry import ComponentRegistry, ModuleSpec

__all__ = ("ActivityKernel", "ComponentRegistry", "GateResult", "GateSnapshot", "KernelAccess",
           "KernelConfig", "KernelError", "ModuleSpec", "Preparation", "PreparedActivity")
