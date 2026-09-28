"""Tests for the JSON Schema -> GBNF compiler.

The claim being tested is the strong one the design rests on: with the grammar
active, output that does not conform to the schema cannot be produced. So each
test checks both directions — conforming JSON is accepted, and specific
malformations are rejected.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.db.models import DecisionKind
from app.llm.grammar import GrammarError, json_schema_to_gbnf
from app.llm.schemas import (
    TradeDecision,
    plan_schema,
    research_schema,
    review_schema,
    schema_for,
    trade_schema,
    validate_output,
)
from tests.gbnf_matcher import Matcher

SYMBOLS = ["AAPL", "MSFT", "NVDA"]


@pytest.fixture(scope="module")
def trade_matcher() -> Matcher:
    return Matcher(json_schema_to_gbnf(trade_schema(SYMBOLS, max_orders=3)))


def test_grammar_accepts_a_well_formed_decision(trade_matcher):
    payload = {
        "market_view": "Breadth is improving.",
        "reasoning": "Both names cleared their 50-day with volume.",
        "orders": [
            {"symbol": "AAPL", "action": "BUY", "conviction": 0.8, "rationale": "Trend intact."},
            {"symbol": "MSFT", "action": "SELL", "conviction": 0.3, "rationale": "Taking profit."},
        ],
    }
    assert trade_matcher.matches(json.dumps(payload))


def test_grammar_accepts_an_empty_order_list(trade_matcher):
    """Holding is a decision; the schema must not force a trade."""
    payload = {"market_view": "Choppy.", "reasoning": "Nothing meets my criteria.", "orders": []}
    assert trade_matcher.matches(json.dumps(payload))


def test_grammar_output_is_parseable_json(trade_matcher):
    payload = {"market_view": "a", "reasoning": "b", "orders": []}
    text = json.dumps(payload)
    assert trade_matcher.matches(text)
    assert json.loads(text) == payload


def test_grammar_rejects_an_unknown_ticker(trade_matcher):
    """The symbol enum makes hallucinating a ticker structurally impossible."""
    payload = {
        "market_view": "x",
        "reasoning": "y",
        "orders": [
            {"symbol": "FAKE", "action": "BUY", "conviction": 0.5, "rationale": "z"}
        ],
    }
    assert not trade_matcher.matches(json.dumps(payload))


def test_grammar_rejects_an_invalid_action(trade_matcher):
    payload = {
        "market_view": "x",
        "reasoning": "y",
        "orders": [
            {"symbol": "AAPL", "action": "HOLD", "conviction": 0.5, "rationale": "z"}
        ],
    }
    assert not trade_matcher.matches(json.dumps(payload))


def test_grammar_rejects_a_missing_key(trade_matcher):
    assert not trade_matcher.matches(json.dumps({"market_view": "x", "reasoning": "y"}))


def test_grammar_rejects_an_extra_key(trade_matcher):
    payload = {"market_view": "x", "reasoning": "y", "orders": [], "extra": 1}
    assert not trade_matcher.matches(json.dumps(payload))


def test_grammar_rejects_prose_wrapped_around_the_json(trade_matcher):
    """The classic local-model failure: a helpful sentence before the JSON."""
    payload = json.dumps({"market_view": "x", "reasoning": "y", "orders": []})
    assert not trade_matcher.matches("Here is my decision:\n" + payload)
    assert not trade_matcher.matches("```json\n" + payload + "\n```")


def test_grammar_rejects_truncated_json(trade_matcher):
    assert not trade_matcher.matches('{"market_view": "x", "reasoning": "y", "orders": [')


def test_grammar_enforces_the_max_orders_bound(trade_matcher):
    def order(symbol):
        return {"symbol": symbol, "action": "BUY", "conviction": 0.5, "rationale": "r"}

    three = {"market_view": "x", "reasoning": "y", "orders": [order(s) for s in SYMBOLS]}
    assert trade_matcher.matches(json.dumps(three))

    four = {
        "market_view": "x",
        "reasoning": "y",
        "orders": [order(s) for s in SYMBOLS] + [order("AAPL")],
    }
    assert not trade_matcher.matches(json.dumps(four))


def test_grammar_accepts_escaped_characters_in_free_text(trade_matcher):
    payload = {
        "market_view": 'He said "buy the dip"',
        "reasoning": "Line one\nLine two\tTabbed \\ backslash",
        "orders": [],
    }
    text = json.dumps(payload)
    assert trade_matcher.matches(text)
    assert json.loads(text)["market_view"] == 'He said "buy the dip"'


def test_grammar_accepts_negative_and_exponent_numbers():
    schema = {
        "type": "object",
        "properties": {"value": {"type": "number"}},
        "required": ["value"],
    }
    matcher = Matcher(json_schema_to_gbnf(schema))
    for value in ("0", "-1", "1.5", "-0.25", "1e3", "-2.5e-4"):
        assert matcher.matches(f'{{"value": {value}}}'), value
    for bad in ("01", "+1", ".5", "1.", "NaN"):
        assert not matcher.matches(f'{{"value": {bad}}}'), bad


def test_grammar_handles_whitespace_variants(trade_matcher):
    compact = '{"market_view":"x","reasoning":"y","orders":[]}'
    spaced = '{ "market_view" : "x" , "reasoning" : "y" , "orders" : [ ] }'
    indented = json.dumps(
        {"market_view": "x", "reasoning": "y", "orders": []}, indent=2
    )
    assert trade_matcher.matches(compact)
    assert trade_matcher.matches(spaced)
    assert trade_matcher.matches(indented)


@pytest.mark.parametrize(
    "schema",
    [trade_schema(SYMBOLS), plan_schema(SYMBOLS), review_schema(), research_schema()],
    ids=["trade", "plan", "review", "research"],
)
def test_every_phase_schema_compiles_and_round_trips(schema):
    grammar = json_schema_to_gbnf(schema)
    matcher = Matcher(grammar)
    sample = _sample_for(schema)
    assert matcher.matches(json.dumps(sample))


def _sample_for(schema: dict) -> dict:
    """Build the minimal conforming document for a schema."""
    out = {}
    for key, spec in schema["properties"].items():
        if spec.get("type") == "array":
            out[key] = []
        elif spec.get("type") == "number":
            out[key] = 0.5
        elif "enum" in spec:
            out[key] = spec["enum"][0]
        else:
            out[key] = "text"
    return out


def test_boolean_and_null_types_compile():
    schema = {
        "type": "object",
        "properties": {
            "flag": {"type": "boolean"},
            "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["flag", "maybe"],
    }
    matcher = Matcher(json_schema_to_gbnf(schema))
    assert matcher.matches('{"flag": true, "maybe": null}')
    assert matcher.matches('{"flag": false, "maybe": "hi"}')
    assert not matcher.matches('{"flag": "true", "maybe": null}')


def test_refs_are_resolved():
    schema = {
        "type": "object",
        "$defs": {"Leg": {"type": "object", "properties": {"s": {"type": "string"}}, "required": ["s"]}},
        "properties": {"leg": {"$ref": "#/$defs/Leg"}},
        "required": ["leg"],
    }
    matcher = Matcher(json_schema_to_gbnf(schema))
    assert matcher.matches('{"leg": {"s": "x"}}')


def test_unsupported_schema_raises_rather_than_silently_degrading():
    with pytest.raises(GrammarError):
        json_schema_to_gbnf({"type": "object", "properties": {"x": {"type": "weird"}}, "required": ["x"]})


# -------------------------------------------------- pydantic post-validation


def test_pydantic_enforces_bounds_the_grammar_cannot():
    """GBNF cannot express 0 <= conviction <= 1, so Pydantic must."""
    payload = {
        "market_view": "x",
        "reasoning": "y",
        "orders": [
            {"symbol": "AAPL", "action": "BUY", "conviction": 7.0, "rationale": "z"}
        ],
    }
    matcher = Matcher(json_schema_to_gbnf(trade_schema(SYMBOLS)))
    assert matcher.matches(json.dumps(payload))  # grammatically fine...
    # Named explicitly: a blind `Exception` here would pass just as happily on
    # an import error as on the validation failure being asserted.
    with pytest.raises(ValidationError):  # ...but semantically rejected
        validate_output(DecisionKind.TRADE, payload)


def test_validate_output_normalises_symbols():
    decision = validate_output(
        DecisionKind.TRADE,
        {
            "market_view": "x",
            "reasoning": "y",
            "orders": [{"symbol": " aapl ", "action": "BUY", "conviction": 0.5, "rationale": "z"}],
        },
    )
    assert isinstance(decision, TradeDecision)
    assert decision.orders[0].symbol == "AAPL"


def test_schema_for_covers_every_decision_kind():
    for kind in DecisionKind:
        schema = schema_for(kind, SYMBOLS)
        assert schema["type"] == "object"
        Matcher(json_schema_to_gbnf(schema))
