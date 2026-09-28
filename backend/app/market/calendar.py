"""US equity market calendar and the phase machine built on top of it.

Deliberately a *local* library rather than a broker API: it knows holidays and
early closes without a network round-trip, and it exposes the pre-market and
after-hours boundaries that most broker APIs do not publish at all. The system's
heartbeat should never depend on someone else's endpoint answering at 04:00.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import pandas_market_calendars as mcal

from app.core.logging import get_logger
from app.db.models import Phase

log = get_logger(__name__)

MARKET_TZ = "America/New_York"
UTC = dt.timezone.utc

#: Fallback offsets, used only if the calendar library stops publishing the
#: extended-hours columns. They are exact for NYSE, including half-days
#: (pre = open - 5h30, post = close + 4h).
_PRE_OFFSET = dt.timedelta(hours=5, minutes=30)
_POST_OFFSET = dt.timedelta(hours=4)

_PHASE_ORDER = [Phase.PRE_MARKET, Phase.OPEN, Phase.AFTER_HOURS, Phase.CLOSED]


@dataclass(frozen=True)
class Session:
    """The four boundaries of one trading day, all in UTC."""

    day: dt.date
    pre_open: dt.datetime
    market_open: dt.datetime
    market_close: dt.datetime
    post_close: dt.datetime

    @property
    def is_early_close(self) -> bool:
        local_close = self.market_close.astimezone(_tz())
        return (local_close.hour, local_close.minute) < (16, 0)

    def phase_at(self, when: dt.datetime) -> Phase:
        if when < self.pre_open or when >= self.post_close:
            return Phase.CLOSED
        if when < self.market_open:
            return Phase.PRE_MARKET
        if when < self.market_close:
            return Phase.OPEN
        return Phase.AFTER_HOURS

    def boundaries(self) -> list[tuple[dt.datetime, Phase]]:
        return [
            (self.pre_open, Phase.PRE_MARKET),
            (self.market_open, Phase.OPEN),
            (self.market_close, Phase.AFTER_HOURS),
            (self.post_close, Phase.CLOSED),
        ]

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "pre_open": self.pre_open.isoformat(),
            "market_open": self.market_open.isoformat(),
            "market_close": self.market_close.isoformat(),
            "post_close": self.post_close.isoformat(),
            "is_early_close": self.is_early_close,
        }


def _tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(MARKET_TZ)


def _to_utc(value) -> dt.datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").to_pydatetime()


class MarketCalendar:
    """Thin cached wrapper over the NYSE calendar.

    Sessions are loaded in windows and memoised; a year of sessions is a few
    hundred rows, so the whole thing comfortably lives in memory.
    """

    def __init__(self, name: str = "NYSE") -> None:
        self._calendar = mcal.get_calendar(name)
        self._sessions: dict[dt.date, Session] = {}
        self._loaded_from: Optional[dt.date] = None
        self._loaded_to: Optional[dt.date] = None

    # ------------------------------------------------------------- loading
    def _ensure_loaded(self, day: dt.date) -> None:
        if (
            self._loaded_from is not None
            and self._loaded_to is not None
            and self._loaded_from <= day <= self._loaded_to
        ):
            return
        start = day - dt.timedelta(days=120)
        end = day + dt.timedelta(days=240)
        self._load(start, end)

    def _load(self, start: dt.date, end: dt.date) -> None:
        try:
            schedule = self._calendar.schedule(
                start_date=start,
                end_date=end,
                market_times=["pre", "market_open", "market_close", "post"],
                tz="UTC",
            )
        except (TypeError, ValueError, KeyError):
            # Older/newer library shapes: fall back to the core session and
            # derive the extended-hours boundaries from fixed NYSE offsets.
            schedule = self._calendar.schedule(start_date=start, end_date=end, tz="UTC")

        sessions: dict[dt.date, Session] = {}
        for idx, row in schedule.iterrows():
            day = pd.Timestamp(idx).date()
            market_open = _to_utc(row["market_open"])
            market_close = _to_utc(row["market_close"])
            pre_open = (
                _to_utc(row["pre"])
                if "pre" in schedule.columns and pd.notna(row.get("pre"))
                else market_open - _PRE_OFFSET
            )
            post_close = (
                _to_utc(row["post"])
                if "post" in schedule.columns and pd.notna(row.get("post"))
                else market_close + _POST_OFFSET
            )
            sessions[day] = Session(
                day=day,
                pre_open=pre_open,
                market_open=market_open,
                market_close=market_close,
                post_close=post_close,
            )

        self._sessions = sessions
        self._loaded_from = start
        self._loaded_to = end
        log.debug("calendar.loaded", sessions=len(sessions), start=str(start), end=str(end))

    # -------------------------------------------------------------- queries
    def session_for(self, day: dt.date) -> Optional[Session]:
        self._ensure_loaded(day)
        return self._sessions.get(day)

    def is_trading_day(self, day: dt.date) -> bool:
        return self.session_for(day) is not None

    def session_containing(self, when: dt.datetime) -> Optional[Session]:
        """The session whose [pre_open, post_close) window contains `when`.

        After-hours sessions can run past midnight UTC, so the previous local
        day has to be considered too.
        """
        when = _as_utc(when)
        local_day = when.astimezone(_tz()).date()
        for candidate in (local_day, local_day - dt.timedelta(days=1)):
            session = self.session_for(candidate)
            if session and session.pre_open <= when < session.post_close:
                return session
        return None

    def phase_at(self, when: dt.datetime) -> Phase:
        session = self.session_containing(when)
        return session.phase_at(_as_utc(when)) if session else Phase.CLOSED

    def next_session(self, after: dt.datetime) -> Optional[Session]:
        after = _as_utc(after)
        self._ensure_loaded(after.astimezone(_tz()).date())
        upcoming = [s for s in self._sessions.values() if s.pre_open > after]
        return min(upcoming, key=lambda s: s.pre_open) if upcoming else None

    def previous_session(self, before: dt.datetime) -> Optional[Session]:
        before = _as_utc(before)
        self._ensure_loaded(before.astimezone(_tz()).date())
        past = [s for s in self._sessions.values() if s.market_close <= before]
        return max(past, key=lambda s: s.market_close) if past else None

    def next_transition(self, after: dt.datetime) -> tuple[dt.datetime, Phase]:
        """When the phase next changes, and what it changes to."""
        after = _as_utc(after)
        session = self.session_containing(after)
        if session:
            for boundary, phase in session.boundaries():
                if boundary > after:
                    return boundary, phase
        nxt = self.next_session(after)
        if nxt:
            return nxt.pre_open, Phase.PRE_MARKET
        # Nothing scheduled (calendar window exhausted): re-check in an hour.
        return after + dt.timedelta(hours=1), Phase.CLOSED

    def trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        self._load(start - dt.timedelta(days=5), end + dt.timedelta(days=5))
        return sorted(d for d in self._sessions if start <= d <= end)


def _as_utc(when: dt.datetime) -> dt.datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


# ------------------------------------------------------------------- clocks


class Clock:
    """Time source. Swappable so the orchestrator can be driven by a replay."""

    def now(self) -> dt.datetime:  # pragma: no cover - trivial
        raise NotImplementedError

    @property
    def is_replay(self) -> bool:
        return False


class RealClock(Clock):
    def now(self) -> dt.datetime:
        return dt.datetime.now(UTC)


class ReplayClock(Clock):
    """Virtual clock that walks a historical window at an accelerated rate.

    `speed` is the multiplier against wall-clock time; `advance_to` lets the
    orchestrator jump straight to the next interesting boundary instead of
    simulating dead time.
    """

    def __init__(self, start: dt.datetime, end: dt.datetime, speed: float = 60.0) -> None:
        self._cursor = _as_utc(start)
        self.end = _as_utc(end)
        self.speed = speed

    def now(self) -> dt.datetime:
        return self._cursor

    def advance(self, delta: dt.timedelta) -> dt.datetime:
        self._cursor = min(self._cursor + delta, self.end)
        return self._cursor

    def advance_to(self, when: dt.datetime) -> dt.datetime:
        when = _as_utc(when)
        if when > self._cursor:
            self._cursor = min(when, self.end)
        return self._cursor

    @property
    def finished(self) -> bool:
        return self._cursor >= self.end

    @property
    def is_replay(self) -> bool:
        return True


market_calendar = MarketCalendar()
