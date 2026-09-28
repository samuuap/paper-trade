"""Metrics: the pure maths, the daily rollup and the read-side aggregates.

Layering, and why it is worth keeping:

* `financial` — pure functions, no database, no clock. Unit-tested against
  hand-computed numbers.
* `rollup` — turns equity snapshots, fills and decisions into `metrics_daily`
  rows. The single definition of every metric.
* `queries` — read-side shaping for the API. Reads the rollup rather than
  recomputing, so there is never a second implementation of "Sharpe" to
  disagree with the first.
"""

from app.metrics import financial, queries, rollup

__all__ = ["financial", "queries", "rollup"]
