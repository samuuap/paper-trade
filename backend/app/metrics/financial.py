"""Portfolio statistics, as pure functions.

Nothing here touches the database or the clock. That is deliberate: these are
the numbers the whole project is judged on, so they have to be testable with
hand-computed inputs, the same way the ledger is.

Two conventions applied throughout:

* **Not enough data returns `None`, never `0.0`.** A Sharpe ratio of zero and
  "three days of history" are very different statements, and collapsing them
  is how a leaderboard starts lying. Callers decide how to render `None`.
* **Annualisation uses 252 trading days.** Applied to daily observations only;
  feed these functions anything else and the annualised figures are wrong.

A word of caution that belongs next to the code rather than only in the README:
with a few months of data these statistics are dominated by noise. They are
here to compare agents against the baselines under identical conditions, not
to establish that a strategy works.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

TRADING_DAYS = 252

#: Below this many observations, ratios are noise dressed up as insight.
MIN_OBSERVATIONS = 2


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: Sequence[float], sample: bool = True) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    mean = _mean(values)
    divisor = n - 1 if sample else n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / divisor)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile. `q` in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (q / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


# --------------------------------------------------------------- returns


def daily_returns(equity: Sequence[float]) -> list[float]:
    """Simple period-over-period returns from an equity curve."""
    returns = []
    for previous, current in zip(equity, equity[1:], strict=False):
        if previous <= 0:
            continue
        returns.append(current / previous - 1.0)
    return returns


def cumulative_return(equity: Sequence[float]) -> Optional[float]:
    if len(equity) < 2 or equity[0] <= 0:
        return None
    return equity[-1] / equity[0] - 1.0


def total_return(start: float, end: float) -> Optional[float]:
    if start <= 0:
        return None
    return end / start - 1.0


# ----------------------------------------------------------------- risk


def volatility(returns: Sequence[float], annualise: bool = True) -> Optional[float]:
    deviation = _stdev(returns)
    if deviation is None:
        return None
    return deviation * math.sqrt(TRADING_DAYS) if annualise else deviation


def sharpe(returns: Sequence[float], risk_free_annual: float = 0.0) -> Optional[float]:
    """Annualised Sharpe ratio of daily returns."""
    if len(returns) < MIN_OBSERVATIONS:
        return None
    deviation = _stdev(returns)
    if not deviation:
        return None
    daily_rf = risk_free_annual / TRADING_DAYS
    excess = _mean(returns) - daily_rf
    return (excess / deviation) * math.sqrt(TRADING_DAYS)


def sortino(returns: Sequence[float], risk_free_annual: float = 0.0) -> Optional[float]:
    """Like Sharpe, but only downside deviation counts as risk."""
    if len(returns) < MIN_OBSERVATIONS:
        return None
    daily_rf = risk_free_annual / TRADING_DAYS
    downside = [min(r - daily_rf, 0.0) for r in returns]
    # Population form, and the zeros are kept: days that did not lose money are
    # evidence about the downside, not missing data.
    denominator = math.sqrt(sum(d**2 for d in downside) / len(downside))
    if denominator == 0:
        return None
    excess = _mean(returns) - daily_rf
    return (excess / denominator) * math.sqrt(TRADING_DAYS)


def drawdown_series(equity: Sequence[float]) -> list[float]:
    """Drawdown at each point, as a negative fraction of the running peak."""
    series = []
    peak = float("-inf")
    for value in equity:
        peak = max(peak, value)
        series.append(value / peak - 1.0 if peak > 0 else 0.0)
    return series


def max_drawdown(equity: Sequence[float]) -> tuple[float, int]:
    """Worst peak-to-trough fall, and how many observations it lasted.

    Duration is measured from the peak to the point the drawdown is recovered,
    or to the end of the series if it never is — an underwater period that is
    still open is part of the cost of holding it.
    """
    if not equity:
        return 0.0, 0
    worst = 0.0
    longest = 0
    peak = equity[0]
    peak_index = 0
    for index, value in enumerate(equity):
        if value >= peak:
            peak = value
            peak_index = index
            continue
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
        longest = max(longest, index - peak_index)
    return worst, longest


def current_drawdown(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    peak = max(equity)
    return equity[-1] / peak - 1.0 if peak > 0 else 0.0


def calmar(annualised_return: Optional[float], max_dd: float) -> Optional[float]:
    if annualised_return is None or max_dd >= 0:
        return None
    return annualised_return / abs(max_dd)


def annualised_return(cumulative: Optional[float], periods: int) -> Optional[float]:
    """Geometric annualisation of a cumulative return over `periods` days."""
    if cumulative is None or periods <= 0 or cumulative <= -1:
        return None
    return (1.0 + cumulative) ** (TRADING_DAYS / periods) - 1.0


def var_95(returns: Sequence[float]) -> Optional[float]:
    """Historical 95% value at risk: the 5th percentile of daily returns.

    Returned as a negative number, so it reads the same way the loss does.
    """
    if len(returns) < MIN_OBSERVATIONS:
        return None
    return percentile(returns, 5.0)


# ------------------------------------------------------------ benchmark


def beta(returns: Sequence[float], benchmark: Sequence[float]) -> Optional[float]:
    """Sensitivity to the benchmark. Needs both series aligned by day."""
    n = min(len(returns), len(benchmark))
    if n < MIN_OBSERVATIONS:
        return None
    r, b = list(returns[-n:]), list(benchmark[-n:])
    mean_r, mean_b = _mean(r), _mean(b)
    variance = sum((x - mean_b) ** 2 for x in b)
    if variance == 0:
        return None
    covariance = sum((x - mean_r) * (y - mean_b) for x, y in zip(r, b, strict=True))
    return covariance / variance


def alpha(
    returns: Sequence[float],
    benchmark: Sequence[float],
    beta_value: Optional[float] = None,
    risk_free_annual: float = 0.0,
) -> Optional[float]:
    """Annualised Jensen's alpha: the return the benchmark does not explain."""
    n = min(len(returns), len(benchmark))
    if n < MIN_OBSERVATIONS:
        return None
    b = beta_value if beta_value is not None else beta(returns, benchmark)
    if b is None:
        return None
    daily_rf = risk_free_annual / TRADING_DAYS
    excess_portfolio = _mean(returns[-n:]) - daily_rf
    excess_benchmark = _mean(benchmark[-n:]) - daily_rf
    return (excess_portfolio - b * excess_benchmark) * TRADING_DAYS


# ---------------------------------------------------------------- trades


def win_rate(pnls: Sequence[float]) -> Optional[float]:
    if not pnls:
        return None
    return sum(1 for p in pnls if p > 0) / len(pnls)


def profit_factor(pnls: Sequence[float]) -> Optional[float]:
    """Gross profit over gross loss. `None` when there are no losses yet."""
    if not pnls:
        return None
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return None
    return gains / losses


def expectancy(pnls: Sequence[float]) -> Optional[float]:
    """Average realised P&L per closed trade, in currency."""
    if not pnls:
        return None
    return _mean(list(pnls))


# ------------------------------------------------------------- exposure


def concentration_hhi(weights: Sequence[float]) -> float:
    """Herfindahl index of position weights: 1.0 is everything in one name."""
    return sum(abs(w) ** 2 for w in weights)


def turnover(traded_notional: float, average_equity: float) -> float:
    """Notional traded over average equity for the same window."""
    if average_equity <= 0:
        return 0.0
    return traded_notional / average_equity


__all__ = [
    "MIN_OBSERVATIONS",
    "TRADING_DAYS",
    "alpha",
    "annualised_return",
    "beta",
    "calmar",
    "concentration_hhi",
    "cumulative_return",
    "current_drawdown",
    "daily_returns",
    "drawdown_series",
    "expectancy",
    "max_drawdown",
    "percentile",
    "profit_factor",
    "sharpe",
    "sortino",
    "total_return",
    "turnover",
    "var_95",
    "volatility",
    "win_rate",
]
