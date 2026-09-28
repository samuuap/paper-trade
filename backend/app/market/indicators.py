"""Deterministic technical indicators.

This is the "Python calculates" half of the design. Every number an agent sees
about a stock is produced here, in ordinary code that can be unit-tested and
diffed — never by the model. When a strategy loses money you want to be able to
tell a bad calculation apart from bad judgement, and that is only possible if
the calculation never involves the model.

All functions take a chronologically sorted OHLCV frame and are pure.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")

#: Roughly one trading year.
ANNUALISATION_DAYS = 252


def _clean(value: Any) -> Optional[float]:
    """NaN/inf -> None, so nothing unserialisable reaches JSON or the prompt."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    cleaned = _clean(value)
    return None if cleaned is None else round(cleaned, digits)


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI.

    The degenerate windows are handled explicitly rather than left as NaN:
    all-gains is 100, all-losses is 0, and a perfectly flat window is 50.
    """
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    flat = (avg_gain == 0) & (avg_loss == 0)
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_gain == 0) & (avg_loss > 0), 0.0)
    return out.mask(flat, 50.0)


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line, macd_line - signal_line


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = frame["high"], frame["low"], frame["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0):
    middle = sma(series, window)
    std = series.rolling(window=window, min_periods=window).std(ddof=0)
    return middle - num_std * std, middle, middle + num_std * std


def realised_volatility(series: pd.Series, window: int = 20) -> pd.Series:
    returns = np.log(series / series.shift(1))
    return returns.rolling(window=window, min_periods=window).std(ddof=0) * math.sqrt(
        ANNUALISATION_DAYS
    )


def pct_change_over(series: pd.Series, periods: int) -> Optional[float]:
    if len(series) <= periods:
        return None
    past = series.iloc[-periods - 1]
    if past == 0 or pd.isna(past):
        return None
    return float(series.iloc[-1] / past - 1)


def _pct(series: pd.Series, periods: int) -> Optional[float]:
    """Return over `periods` bars, expressed in percent."""
    change = pct_change_over(series, periods)
    return None if change is None else _round(change * 100, 3)


def compute_signals(frame: pd.DataFrame) -> dict[str, Any]:
    """Collapse a price history into the flat signal dict agents and the
    screener consume.

    Returns `{}` for frames too short to say anything meaningful, which callers
    treat as "not screenable yet" rather than as zeros.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"missing OHLCV columns: {missing}")
    if len(frame) < 2:
        return {}

    frame = frame.sort_index()
    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])

    sma20 = sma(close, 20)
    sma50 = sma(close, 50)
    sma200 = sma(close, 200)
    macd_line, macd_signal, macd_hist = macd(close)
    lower_bb, mid_bb, upper_bb = bollinger(close)
    rsi14 = rsi(close)
    atr14 = atr(frame.astype({"high": float, "low": float, "close": float}))
    vol20 = realised_volatility(close)

    avg_volume_20 = volume.rolling(20, min_periods=5).mean()
    last_volume = float(volume.iloc[-1])
    avg_vol = _clean(avg_volume_20.iloc[-1])

    window_252 = close.tail(ANNUALISATION_DAYS)
    high_52w = float(window_252.max())
    low_52w = float(window_252.min())

    sma20_last = _clean(sma20.iloc[-1])
    sma50_last = _clean(sma50.iloc[-1])
    sma200_last = _clean(sma200.iloc[-1])
    bb_lower = _clean(lower_bb.iloc[-1])
    bb_upper = _clean(upper_bb.iloc[-1])
    atr_last = _clean(atr14.iloc[-1])

    signals: dict[str, Any] = {
        "price": _round(last, 4),
        "prev_close": _round(prev, 4),
        "change_pct": _round((last / prev - 1) * 100 if prev else None, 3),
        "bars": int(len(frame)),
        # --- trend
        "sma_20": _round(sma20_last),
        "sma_50": _round(sma50_last),
        "sma_200": _round(sma200_last),
        "above_sma_50": None if sma50_last is None else bool(last > sma50_last),
        "above_sma_200": None if sma200_last is None else bool(last > sma200_last),
        "golden_cross": (
            None
            if sma50_last is None or sma200_last is None
            else bool(sma50_last > sma200_last)
        ),
        "dist_sma_50_pct": _round(
            (last / sma50_last - 1) * 100 if sma50_last else None, 3
        ),
        "dist_sma_200_pct": _round(
            (last / sma200_last - 1) * 100 if sma200_last else None, 3
        ),
        # --- momentum
        "return_5d_pct": _pct(close, 5),
        "return_21d_pct": _pct(close, 21),
        "return_63d_pct": _pct(close, 63),
        "return_126d_pct": _pct(close, 126),
        "rsi_14": _round(rsi14.iloc[-1], 2),
        "macd": _round(macd_line.iloc[-1]),
        "macd_signal": _round(macd_signal.iloc[-1]),
        "macd_hist": _round(macd_hist.iloc[-1]),
        # --- volatility / range
        "atr_14": _round(atr_last),
        "atr_pct": _round((atr_last / last * 100) if atr_last and last else None, 3),
        "volatility_20d": _round(vol20.iloc[-1], 4),
        "bb_lower": _round(bb_lower),
        "bb_middle": _round(_clean(mid_bb.iloc[-1])),
        "bb_upper": _round(bb_upper),
        "bb_position": _round(
            (last - bb_lower) / (bb_upper - bb_lower)
            if bb_lower is not None and bb_upper is not None and bb_upper > bb_lower
            else None,
            4,
        ),
        # --- volume
        "volume": _round(last_volume, 0),
        "avg_volume_20": _round(avg_vol, 0),
        "volume_ratio": _round(last_volume / avg_vol if avg_vol else None, 3),
        "dollar_volume": _round(last_volume * last, 0),
        # --- position in the yearly range
        "high_52w": _round(high_52w),
        "low_52w": _round(low_52w),
        "pct_from_52w_high": _round(
            (last / high_52w - 1) * 100 if high_52w else None, 3
        ),
        "pct_from_52w_low": _round((last / low_52w - 1) * 100 if low_52w else None, 3),
        "range_position_52w": _round(
            (last - low_52w) / (high_52w - low_52w) if high_52w > low_52w else None, 4
        ),
    }

    signals["momentum_score"] = _round(_momentum_score(signals), 4)
    signals["value_score"] = _round(_value_score(signals), 4)
    signals["reversion_score"] = _round(_reversion_score(signals), 4)
    return signals


# ------------------------------------------------------ composite scorecards
# Strategy-flavoured summaries. These exist so the LLM gets one comparable
# number per candidate rather than having to weigh eight raw indicators, which
# is exactly the sort of arithmetic that should not be left to a model.


def _z(value: Optional[float], centre: float, scale: float) -> float:
    if value is None or scale == 0:
        return 0.0
    return max(-3.0, min(3.0, (value - centre) / scale))


def _momentum_score(s: dict[str, Any]) -> float:
    """Trend strength: recent returns, trend alignment, volume confirmation."""
    score = 0.0
    score += 0.30 * _z(s.get("return_21d_pct"), 0.0, 8.0)
    score += 0.25 * _z(s.get("return_63d_pct"), 0.0, 15.0)
    score += 0.15 * _z(s.get("macd_hist"), 0.0, max(abs(s.get("price") or 1) * 0.01, 0.1))
    score += 0.15 * (1.0 if s.get("above_sma_50") else -1.0 if s.get("above_sma_50") is False else 0.0)
    score += 0.10 * (1.0 if s.get("golden_cross") else -1.0 if s.get("golden_cross") is False else 0.0)
    score += 0.05 * _z(s.get("volume_ratio"), 1.0, 0.8)
    return score


def _value_score(s: dict[str, Any]) -> float:
    """Price-derived cheapness proxy.

    The free data tier is thin on fundamentals, so this leans on distance from
    the 52-week high, position in the yearly range and mean-reversion setup
    rather than on multiples. The Value agent is knowingly handicapped here —
    see the risk notes in the README.
    """
    score = 0.0
    score += 0.35 * _z(-(s.get("pct_from_52w_high") or 0.0), 15.0, 15.0)
    score += 0.25 * _z(1.0 - (s.get("range_position_52w") or 0.5), 0.5, 0.3)
    score += 0.20 * (1.0 if s.get("above_sma_200") else 0.0)  # cheap, not broken
    score += 0.10 * _z(-(s.get("volatility_20d") or 0.3), -0.25, 0.15)
    score += 0.10 * _z(s.get("return_126d_pct"), -5.0, 20.0)
    return score


def _reversion_score(s: dict[str, Any]) -> float:
    """How stretched the name is against its own recent behaviour."""
    score = 0.0
    rsi_value = s.get("rsi_14")
    if rsi_value is not None:
        # Symmetric: both oversold and overbought are opportunities for a
        # contrarian, so the magnitude of the stretch is what counts.
        score += 0.45 * min(3.0, abs(50.0 - rsi_value) / 12.0)
    bb_position = s.get("bb_position")
    if bb_position is not None:
        score += 0.30 * min(3.0, abs(bb_position - 0.5) / 0.25)
    score += 0.15 * min(3.0, abs(s.get("return_5d_pct") or 0.0) / 4.0)
    score += 0.10 * _z(s.get("volume_ratio"), 1.0, 0.8)
    return score


def frame_from_bars(bars: list) -> pd.DataFrame:
    """Build the indicator frame from `MarketBar` rows (or any OHLCV objects)."""
    if not bars:
        return pd.DataFrame(columns=list(REQUIRED_COLUMNS))
    records = [
        {
            "ts": b.ts,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume or 0),
        }
        for b in bars
    ]
    frame = pd.DataFrame.from_records(records).set_index("ts").sort_index()
    return frame
