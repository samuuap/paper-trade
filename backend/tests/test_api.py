"""HTTP surface tests.

Driven through the ASGI app rather than a live server, so the whole suite
still runs with nothing to start. The lifespan is deliberately not triggered:
these tests exercise the read surface, and booting the orchestrator inside them
would make every assertion a race.

What is checked is the contract the dashboard depends on — the fields exist,
the shapes are stable, and unknown resources 404 instead of 500.
"""

from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.routes.stream import event_publisher
from app.core.config import settings
from app.core.events import bus
from app.db.models import DecisionKind, Phase
from app.main import app
from app.orchestrator import cycle

API = settings.api_prefix
pytestmark = pytest.mark.usefixtures("roster")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_liveness_and_root(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    root = (await client.get("/")).json()
    assert root["api"] == API


async def test_leaderboard_lists_the_whole_roster_ranked(client):
    rows = (await client.get(f"{API}/leaderboard")).json()
    assert len(rows) == 6
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5, 6]
    # Ranked by return, descending.
    returns = [r["total_return_pct"] for r in rows]
    assert returns == sorted(returns, reverse=True)
    # The economics live alongside the performance, which is the whole point.
    for row in rows:
        assert {"shadow_cost_usd", "inference_seconds", "total_tokens"} <= row.keys()


async def test_agent_detail_exposes_positions_and_prompt(client):
    await cycle.run_cycle(DecisionKind.TRADE, Phase.OPEN, only=["buy_and_hold_spy"])
    body = (await client.get(f"{API}/agents/buy_and_hold_spy")).json()

    assert body["agent"]["kind"] == "BASELINE"
    assert body["portfolio"]["initial_capital"] == settings.initial_capital
    assert [p["symbol"] for p in body["positions"]] == ["SPY"]
    assert body["positions"][0]["weight_pct"] == pytest.approx(100.0, abs=0.5)


async def test_an_unknown_agent_is_a_404(client):
    response = await client.get(f"{API}/agents/does-not-exist")
    assert response.status_code == 404


async def test_the_trade_log_carries_the_rationale(client):
    await cycle.run_cycle(DecisionKind.TRADE, Phase.OPEN, only=["quant_sma"])
    trades = (await client.get(f"{API}/trades?limit=5")).json()
    assert trades
    assert all(t["rationale"] for t in trades)
    assert all(t["decision_id"] for t in trades)


async def test_rejected_orders_explain_themselves(client):
    """The audit trail for a refusal has to say why, not just that."""
    await cycle.run_cycle(DecisionKind.TRADE, Phase.OPEN, only=["quant_sma"])
    # Second pass: the slots are full, so at least one buy is refused.
    await cycle.run_cycle(DecisionKind.TRADE, Phase.OPEN, only=["quant_sma"])
    rows = (await client.get(f"{API}/orders/rejected")).json()
    for row in rows:
        assert row["reject_reason"]
        assert "symbol" in row["risk_notes"]


async def test_screening_through_the_api_matches_the_agents_view(client):
    body = (await client.get(f"{API}/market/screen?preset=momentum&limit=5")).json()
    assert body["preset"] == "momentum"
    assert 0 < len(body["candidates"]) <= 5
    assert all("momentum_score" in c for c in body["candidates"])


async def test_an_unknown_preset_is_a_400_not_a_silent_default(client):
    response = await client.get(f"{API}/market/screen?preset=nonsense")
    assert response.status_code == 400


async def test_health_covers_every_way_this_can_break(client):
    body = (await client.get(f"{API}/health")).json()
    assert {
        "orchestrator",
        "data",
        "provider",
        "inference",
        "decisions",
        "jobs",
        "stream",
    } <= body.keys()
    assert body["inference"]["memory"]["total_gb"] > 0
    assert body["data"]["universe_size"] > 0


async def test_status_reports_the_phase_and_the_next_transition(client):
    body = (await client.get(f"{API}/status")).json()
    assert body["phase"] in {p.value for p in Phase}
    assert body["next_transition"]["phase"] in {p.value for p in Phase}
    assert body["tasks"]


async def test_equity_curves_include_the_rebased_benchmark(client):
    await cycle.snapshot_all_equity(Phase.OPEN)
    body = (await client.get(f"{API}/equity")).json()
    assert len(body["series"]) == 6
    assert body["benchmark"]["symbol"] == "SPY"
    # Rebased onto the same starting capital so the chart is comparable.
    assert body["benchmark"]["points"][0]["equity"] == pytest.approx(
        settings.initial_capital
    )


async def test_equity_series_come_back_in_a_stable_order(client):
    """The dashboard styles the grey baselines by arrival order.

    An unordered query lets the database return them either way round, and the
    two reference curves would then swap dash patterns between reloads.
    """
    await cycle.snapshot_all_equity(Phase.OPEN)
    first = [s["slug"] for s in (await client.get(f"{API}/equity")).json()["series"]]
    second = [s["slug"] for s in (await client.get(f"{API}/equity")).json()["series"]]
    assert first == second == sorted(first)


async def test_the_benchmark_is_clipped_to_the_window_the_agents_cover(client):
    """Regression: a year of SPY against a day of equity squashes the chart.

    The bar history goes back much further than the first equity snapshot, so
    an unclipped overlay compresses every agent curve into the right-hand edge
    and the comparison the chart exists for becomes unreadable.
    """
    await cycle.snapshot_all_equity(Phase.OPEN)
    body = (await client.get(f"{API}/equity")).json()

    agent_points = [p["ts"] for s in body["series"] for p in s["points"]]
    assert agent_points, "expected at least one equity snapshot"
    first_agent = min(agent_points)
    benchmark_points = body["benchmark"]["points"]
    assert benchmark_points, "expected benchmark bars in the window"

    # One day of slack: the bar covering the first snapshot's day is stamped
    # at midnight UTC, before the snapshot itself, and is the rebasing point.
    oldest = min(p["ts"] for p in benchmark_points)
    assert oldest >= (
        dt.datetime.fromisoformat(first_agent) - dt.timedelta(days=2)
    ).isoformat()


async def test_the_stream_replays_recent_events_on_connect():
    """A page load between 15-minute cycles must not show an empty dashboard."""
    bus.publish("decision", agent="momentum", status="OK")

    frames = []
    publisher = event_publisher(replay=5, keepalive_seconds=0.05)
    try:
        async for frame in publisher:
            if frame["event"] == "keepalive":
                break
            frames.append(frame)
    finally:
        await publisher.aclose()

    assert [f["event"] for f in frames] == ["decision"]
    assert "momentum" in frames[0]["data"]


async def test_the_stream_filters_by_topic():
    bus.publish("job", job="market.quotes", status="ok")
    bus.publish("decision", agent="value", status="OK")

    frames = []
    publisher = event_publisher(topics={"decision"}, replay=10, keepalive_seconds=0.05)
    try:
        async for frame in publisher:
            if frame["event"] == "keepalive":
                break
            frames.append(frame)
    finally:
        await publisher.aclose()

    assert frames
    assert {f["event"] for f in frames} == {"decision"}


async def test_the_stream_emits_a_keepalive_while_nothing_happens():
    """Without this an idle connection gets reaped by a proxy mid-session."""
    publisher = event_publisher(replay=0, keepalive_seconds=0.05)
    try:
        frame = await anext(publisher)
        # The generator must survive its own keepalive: cancelling the wrong
        # awaitable here used to break the stream on the very first one.
        second = await anext(publisher)
    finally:
        await publisher.aclose()

    assert frame["event"] == "keepalive"
    assert second["event"] == "keepalive"


async def test_the_stream_endpoint_is_reachable(client):
    body = (await client.get(f"{API}/stream/topics")).json()
    assert "decision" in body["topics"]
