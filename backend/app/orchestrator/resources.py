"""Resource guard: what this machine can afford to run right now.

The system shares a laptop with its owner, so "can I run inference" is a real
question and not a formality. Two jobs:

* **Pick the model for the phase.** A ~14B model during market hours, when the
  machine is probably in use; the larger MoE overnight and at weekends, when it
  is not. That is the whole point of tiering by phase.
* **Refuse to schedule inference when memory is tight.** A swapping laptop is
  slower than no decision at all, and a decision that arrives twenty minutes
  late is worse than one that is skipped and recorded as skipped.

The "would the big model fit" check deliberately measures the inference
server's own resident memory and adds it back before deciding. Without that,
loading the large model would immediately make itself look unaffordable and the
guard would flap between the two models on every cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import psutil

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import Phase

log = get_logger(__name__)

GB = 1024**3

#: Phases where the machine is likely idle, so the larger model is worth loading.
DEEP_PHASES = {Phase.CLOSED}


@dataclass(frozen=True)
class MemorySnapshot:
    total_gb: float
    available_gb: float
    used_pct: float

    def to_dict(self) -> dict:
        return {
            "total_gb": self.total_gb,
            "available_gb": self.available_gb,
            "used_pct": self.used_pct,
        }


@dataclass(frozen=True)
class InferenceProcess:
    """The local inference server, if it is running."""

    pid: int
    name: str
    rss_gb: float

    def to_dict(self) -> dict:
        return {"pid": self.pid, "name": self.name, "rss_gb": self.rss_gb}


@dataclass(frozen=True)
class ResourceVerdict:
    allowed: bool
    reason: Optional[str]
    memory: MemorySnapshot

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "memory": self.memory.to_dict()}


def memory() -> MemorySnapshot:
    vm = psutil.virtual_memory()
    return MemorySnapshot(
        total_gb=round(vm.total / GB, 2),
        available_gb=round(vm.available / GB, 2),
        used_pct=round(vm.percent, 1),
    )


def inference_process() -> Optional[InferenceProcess]:
    """Find the local inference server and report what it is holding.

    Also answers the dashboard's "is the model still resident, or has it been
    unloaded" question, which is otherwise invisible from outside the server.
    """
    needle = settings.inference_process_name.lower()
    if not needle:
        return None
    try:
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            name = (proc.info.get("name") or "").lower()
            if needle not in name:
                continue
            info = proc.info.get("memory_info")
            return InferenceProcess(
                pid=proc.info["pid"],
                name=proc.info.get("name") or needle,
                rss_gb=round((info.rss if info else 0) / GB, 2),
            )
    except (psutil.Error, OSError) as exc:  # pragma: no cover - platform dependent
        log.debug("resources.process_scan_failed", error=str(exc))
    return None


def estimated_model_gb(model: str) -> float:
    return (
        settings.model_memory_estimate_closed_gb
        if model == settings.llm_model_closed
        else settings.model_memory_estimate_open_gb
    )


def model_for_phase(phase: Phase) -> str:
    """Which model to serve in `phase`, downgraded if it would not fit.

    Called on every LLM turn, so it must be cheap and must not oscillate.
    """
    if phase not in DEEP_PHASES:
        return settings.llm_model_open

    wanted = settings.llm_model_closed
    needed = settings.model_memory_estimate_closed_gb + settings.min_free_memory_gb

    snapshot = memory()
    if needed > snapshot.total_gb:
        # Not a transient condition: this model will never fit on this machine.
        log.warning(
            "resources.model_does_not_fit",
            model=wanted,
            needed_gb=needed,
            total_gb=snapshot.total_gb,
        )
        return settings.llm_model_open

    # Memory the server is already holding would come back on a swap, so count
    # it as available rather than letting the model veto its own successor.
    resident = inference_process()
    headroom = snapshot.available_gb + (resident.rss_gb if resident else 0.0)
    if headroom < needed:
        log.info(
            "resources.model_downgraded",
            wanted=wanted,
            fallback=settings.llm_model_open,
            headroom_gb=round(headroom, 2),
            needed_gb=needed,
        )
        return settings.llm_model_open
    return wanted


def check_inference() -> ResourceVerdict:
    """Whether a decision may call the model at all right now."""
    snapshot = memory()
    if snapshot.available_gb < settings.min_free_memory_gb:
        return ResourceVerdict(
            allowed=False,
            reason=(
                f"only {snapshot.available_gb:.1f} GB free, "
                f"below the {settings.min_free_memory_gb:.1f} GB floor"
            ),
            memory=snapshot,
        )
    return ResourceVerdict(allowed=True, reason=None, memory=snapshot)


def snapshot(phase: Optional[Phase] = None) -> dict:
    """Everything the health endpoint needs about local inference capacity."""
    from app.llm.queue import inference_queue

    resident = inference_process()
    return {
        "memory": memory().to_dict(),
        "min_free_memory_gb": settings.min_free_memory_gb,
        "inference_process": resident.to_dict() if resident else None,
        "model_resident": resident is not None,
        "model_open": settings.llm_model_open,
        "model_closed": settings.llm_model_closed,
        "model_for_phase": model_for_phase(phase) if phase is not None else None,
        "queue": inference_queue.stats().to_dict(),
    }
