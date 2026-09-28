"""Cost accounting for local inference.

Running the model locally does not make a decision free — it moves the cost from
dollars to time and watt-hours. Two numbers are recorded for every decision so
that trade-off can be argued with data instead of vibes:

* **shadow cost** — what the identical call would have cost on the frontier API.
  This is the number that answers "would paying for Claude have been cheaper
  than the latency I'm eating?"
* **energy cost** — inference seconds times the machine's draw times the
  electricity tariff. Small, but it is the honest local counterpart.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings

MILLION = 1_000_000


@dataclass(frozen=True)
class CostBreakdown:
    shadow_usd: float
    energy_usd: float
    model: str

    @property
    def total_usd(self) -> float:
        return round(self.shadow_usd + self.energy_usd, 6)


def shadow_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """USD the same token counts would have cost on the reference API."""
    inbound = (prompt_tokens / MILLION) * settings.shadow_cost_input_per_mtok
    outbound = (completion_tokens / MILLION) * settings.shadow_cost_output_per_mtok
    return round(inbound + outbound, 6)


def energy_cost(inference_seconds: float) -> float:
    kwh = (settings.inference_watts * max(inference_seconds, 0.0)) / 3600 / 1000
    return round(kwh * settings.electricity_cost_per_kwh, 8)


def breakdown(prompt_tokens: int, completion_tokens: int, inference_seconds: float) -> CostBreakdown:
    return CostBreakdown(
        shadow_usd=shadow_cost(prompt_tokens, completion_tokens),
        energy_usd=energy_cost(inference_seconds),
        model=settings.shadow_model_name,
    )


def compute_seconds_per_pnl_dollar(inference_seconds: float, pnl: float) -> float | None:
    """Seconds of compute burned per dollar of P&L produced.

    The local equivalent of "does this agent pay for itself". Undefined when
    P&L is zero or negative, and deliberately returns `None` there rather than
    a misleading infinity.
    """
    if pnl <= 0:
        return None
    return round(inference_seconds / pnl, 4)
