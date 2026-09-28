"""The 24/7 orchestration layer.

* `resources` — can this machine afford to run inference right now, and which
  model should this phase use.
* `jobs` — every scheduled unit of work leaves a `job_runs` trace.
* `ingest` — market data in: daily bars for the universe, quotes and news for
  the active set.
* `cycle` — one turn of the whole roster, one transaction per agent.
* `scheduler` — the phase machine that decides when any of the above happens.
"""

from app.orchestrator import cycle, ingest, jobs, resources, scheduler
from app.orchestrator.scheduler import Orchestrator, get_orchestrator, set_orchestrator

__all__ = [
    "Orchestrator",
    "cycle",
    "get_orchestrator",
    "ingest",
    "jobs",
    "resources",
    "scheduler",
    "set_orchestrator",
]
