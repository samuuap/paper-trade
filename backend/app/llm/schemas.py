"""Structured decision schemas.

Each phase asks the model for a different, narrow artefact. The JSON schemas are
written by hand rather than derived from Pydantic so that key order, item
bounds and — most importantly — the *symbol enum* are under our control.

Constraining `symbol` to the exact candidate list is the single highest-leverage
use of the grammar here: it makes hallucinating a ticker structurally
impossible, so the model can pick badly but never pick something that does not
exist.

The Pydantic models re-validate whatever comes back. The grammar guarantees
shape; Pydantic enforces the things a grammar cannot express, such as
`0 <= conviction <= 1`.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.db.models import DecisionKind

MAX_TEXT = 2000


# ------------------------------------------------------------- pydantic side


class OrderProposal(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    conviction: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = ""

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class TradeDecision(BaseModel):
    market_view: str = ""
    reasoning: str = ""
    orders: list[OrderProposal] = Field(default_factory=list)


class PlanDecision(BaseModel):
    market_view: str = ""
    reasoning: str = ""
    watchlist: list[str] = Field(default_factory=list)
    plan: str = ""

    @field_validator("watchlist")
    @classmethod
    def _upper_all(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s and s.strip()]


class ReviewDecision(BaseModel):
    summary: str = ""
    attribution: str = ""
    lessons: str = ""
    journal: str = ""


class ResearchDecision(BaseModel):
    summary: str = ""
    hypotheses: list[str] = Field(default_factory=list)
    strategy_notes: str = ""


DECISION_MODELS: dict[DecisionKind, type[BaseModel]] = {
    DecisionKind.TRADE: TradeDecision,
    DecisionKind.PLAN: PlanDecision,
    DecisionKind.REVIEW: ReviewDecision,
    DecisionKind.RESEARCH: ResearchDecision,
}


# --------------------------------------------------------- json schema side


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _symbol_property(symbols: Optional[list[str]]) -> dict[str, Any]:
    """A ticker field, pinned to the candidate list when we have one."""
    if symbols:
        return {
            "type": "string",
            "enum": sorted(dict.fromkeys(s.upper() for s in symbols)),
            "description": "Ticker, chosen from the candidates you were shown.",
        }
    return {"type": "string", "description": "Ticker symbol."}


def trade_schema(symbols: Optional[list[str]] = None, max_orders: int = 5) -> dict[str, Any]:
    return {
        "type": "object",
        "title": "TradeDecision",
        "properties": {
            "market_view": _string(
                "One sentence on what the market and your candidates are doing."
            ),
            "reasoning": _string(
                "Why you are taking these actions, grounded in the signals you were given."
            ),
            "orders": {
                "type": "array",
                "minItems": 0,
                "maxItems": max(1, max_orders),
                "description": "Orders to place. An empty list means hold.",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": _symbol_property(symbols),
                        "action": {"type": "string", "enum": ["BUY", "SELL"]},
                        "conviction": {
                            "type": "number",
                            "description": "0 to 1. Scales the position size.",
                        },
                        "rationale": _string("One line: why this specific trade."),
                    },
                    "required": ["symbol", "action", "conviction", "rationale"],
                },
            },
        },
        "required": ["market_view", "reasoning", "orders"],
    }


def plan_schema(symbols: Optional[list[str]] = None, max_watchlist: int = 10) -> dict[str, Any]:
    return {
        "type": "object",
        "title": "PlanDecision",
        "properties": {
            "market_view": _string("What the overnight news and gaps tell you."),
            "reasoning": _string("How that shapes today's approach for your strategy."),
            "watchlist": {
                "type": "array",
                "minItems": 0,
                "maxItems": max(1, max_watchlist),
                "items": _symbol_property(symbols),
            },
            "plan": _string("Concrete plan for the session, in two or three sentences."),
        },
        "required": ["market_view", "reasoning", "watchlist", "plan"],
    }


def review_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "title": "ReviewDecision",
        "properties": {
            "summary": _string("How the session went for your portfolio."),
            "attribution": _string("Which positions drove the P&L, and why."),
            "lessons": _string("What you would do differently."),
            "journal": _string("Free-form diary entry for the record."),
        },
        "required": ["summary", "attribution", "lessons", "journal"],
    }


def research_schema(max_hypotheses: int = 5) -> dict[str, Any]:
    return {
        "type": "object",
        "title": "ResearchDecision",
        "properties": {
            "summary": _string("What you looked into and what you found."),
            "hypotheses": {
                "type": "array",
                "minItems": 0,
                "maxItems": max(1, max_hypotheses),
                "items": _string("A testable idea about the market or your strategy."),
            },
            "strategy_notes": _string("Adjustments you want to carry into next week."),
        },
        "required": ["summary", "hypotheses", "strategy_notes"],
    }


def schema_for(
    kind: DecisionKind,
    symbols: Optional[list[str]] = None,
    max_orders: int = 5,
) -> dict[str, Any]:
    if kind is DecisionKind.TRADE:
        return trade_schema(symbols, max_orders)
    if kind is DecisionKind.PLAN:
        return plan_schema(symbols)
    if kind is DecisionKind.REVIEW:
        return review_schema()
    return research_schema()


def validate_output(kind: DecisionKind, payload: dict[str, Any]) -> BaseModel:
    """Re-validate parsed output against the Pydantic model for `kind`."""
    model = DECISION_MODELS[kind]
    return model.model_validate(payload)
