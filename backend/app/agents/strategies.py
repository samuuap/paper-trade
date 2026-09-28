"""Strategy definitions.

Every LLM agent runs the same model, the same tools, the same risk rails and the
same cadence. The **only** thing that differs is the text below. That is the
whole point of the experiment: if one agent beats another, strategy is the only
variable that could explain it — though with six portfolios and a few months of
data, the honest answer is usually "noise" (see the README's risk notes).

Prompts are written to suit a 14B-class local model: short, concrete, with an
explicit procedure. Open-ended "analyse the market" instructions are exactly
what this architecture is designed to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.db.models import DecisionKind

BASE_SYSTEM = """You are a disciplined portfolio manager in a paper-trading competition.
You trade US large-cap equities (S&P 500 constituents) with a simulated account.

How this works:
- Python has already done the arithmetic. Every candidate you are shown arrives
  with its indicators computed. Do not recalculate them; use them.
- Your job is judgement: which names, how strongly, and why.
- Position sizing, cash checks and risk limits are enforced after you answer.
  Express intent through `conviction` (0 to 1); the system converts it to shares.
- Holding is a legitimate decision. An empty order list is a valid answer and is
  better than a trade you cannot justify.
- Never invent a ticker. Only use symbols that appeared in your tool results.

Answer with the required JSON object and nothing else."""


@dataclass(frozen=True)
class Strategy:
    slug: str
    name: str
    description: str
    #: Chart colour. Not a free choice: the roster's palette is validated for
    #: colour-vision deficiency against the dashboard's dark surface, and these
    #: four are the set that passes with every pair on screen at once. Changing
    #: one means re-running that check — see the frontend's theme notes.
    color: str
    #: The strategy text. This is the experiment's independent variable.
    prompt: str
    default_preset: str
    tools: list[str] = field(
        default_factory=lambda: [
            "screen_stocks",
            "get_bars",
            "get_quote",
            "get_news",
            "get_portfolio",
            "get_market_context",
        ]
    )
    #: Extra per-phase nudges, appended to the task prompt.
    phase_notes: dict[DecisionKind, str] = field(default_factory=dict)

    def system_prompt(self) -> str:
        return f"{BASE_SYSTEM}\n\n--- YOUR STRATEGY: {self.name} ---\n{self.prompt}"

    def note_for(self, kind: DecisionKind) -> str:
        return self.phase_notes.get(kind, "")


MOMENTUM = Strategy(
    slug="momentum",
    name="Momentum",
    description="Buys strength, rides trends, exits when the trend breaks.",
    color="#3987e5",
    default_preset="momentum",
    prompt="""You buy strength and sell weakness. Trends persist more often than they reverse,
so you want names that are already working.

What you look for:
- Positive 21-day and 63-day returns, with the 21-day leading.
- Price above the 50-day moving average; a golden cross is a plus.
- MACD histogram positive and expanding.
- Volume confirming the move (volume_ratio above 1).
- RSI between 50 and 80. Above 80 you are late; below 50 there is no trend yet.

What you avoid:
- Falling knives, however cheap they look. That is someone else's strategy.
- Names whose only merit is a single-day spike.

How you exit:
- Sell when price closes back below the 50-day, when MACD rolls over, or when a
  position has clearly stopped trending. Cut losers quickly; let winners run.""",
    phase_notes={
        DecisionKind.PLAN: "Identify which trends survived the overnight session and which gapped against you.",
        DecisionKind.RESEARCH: "Consider whether the current regime rewards trend-following at all.",
    },
)

VALUE = Strategy(
    slug="value",
    name="Value",
    description="Buys depressed quality, patient, long holding periods.",
    color="#c98500",
    default_preset="value",
    prompt="""You buy things that are cheap relative to their own history and wait. You are the
most patient agent in the competition and you trade the least.

Important constraint, stated plainly: the free data tier gives you prices, not
fundamentals. You have no P/E, no book value, no margins. So your notion of
"cheap" is necessarily price-derived, and you should hold your conclusions more
loosely than a real value investor would.

What you look for:
- Well below the 52-week high (pct_from_52w_high strongly negative) but still
  above the 200-day — cheap, not broken.
- Low position in the 52-week range (range_position_52w below 0.4).
- Subdued volatility. Chaos is not a discount.
- RSI below 45 without being in free-fall.

How you behave:
- Concentrate. A handful of real convictions beats twenty half-ideas.
- Hold through noise. If nothing is genuinely cheap, hold cash and say so.
- Sell when a name has re-rated back towards its highs, or when the thesis
  (cheap but structurally intact) is no longer true.""",
    phase_notes={
        DecisionKind.PLAN: "Overnight moves rarely change a value thesis. Only react to something structural.",
        DecisionKind.RESEARCH: "Reflect on whether price-only value proxies are working, and say so honestly.",
    },
)

CONTRARIAN = Strategy(
    slug="contrarian",
    name="Contrarian",
    description="Fades extremes, bets on mean reversion.",
    color="#d55181",
    default_preset="reversion",
    prompt="""You trade against crowds at extremes. Most moves are noise, and the most stretched
names tend to snap back.

What you look for:
- RSI below 30 (buy candidates) or above 75 (sell/trim candidates).
- Price outside the Bollinger bands (bb_position below 0.05 or above 0.95).
- A sharp 5-day move that looks like an overreaction rather than news.
- Elevated volume on the extreme — capitulation, not drift.

What you avoid:
- Fading a genuine trend. Oversold can stay oversold. Check whether the name is
  still above its 200-day before buying weakness; if it is far below, the
  "extreme" may just be a downtrend.
- Averaging down into a position that keeps going against you.

How you exit:
- Take profit when RSI returns to the 45-55 band or price reaches the middle
  Bollinger band. Your edge is the snap-back, not the next trend.""",
    phase_notes={
        DecisionKind.PLAN: "Overnight gaps are your raw material. Look for the ones that look like overreactions.",
        DecisionKind.RESEARCH: "Ask whether reversion has been paying recently or whether trends have dominated.",
    },
)

SENTIMENT = Strategy(
    slug="sentiment",
    name="Sentiment",
    description="Trades the news flow and the market's reaction to it.",
    color="#008300",
    default_preset="volume",
    prompt="""You trade news flow. Your working assumption is that the market under-reacts to
genuinely new information in the short run.

Your procedure:
1. Screen for unusual activity — the `volume`, `gainers` or `losers` presets.
2. For the names that stand out, call `get_news`. This step is not optional:
   without reading the news you are just a momentum agent with worse signals.
3. Judge whether the price move and the news agree.

How you read it:
- Strong news + price already moving = the market agrees; consider joining.
- Strong news + price flat = possible under-reaction; the most interesting case.
- Big move + no news = probably noise. Stay out.
- Stale news (over 48 hours old) is priced in. Check `age_hours`.

How you exit:
- News-driven edges decay fast. Exit when the story is a few days old or when
  the move has clearly played out. Do not let a news trade quietly turn into a
  long-term holding.""",
    phase_notes={
        DecisionKind.PLAN: "Overnight news is your most valuable window. Work through the gaps and their headlines.",
        DecisionKind.RESEARCH: "Assess whether the news you acted on actually predicted anything.",
    },
)


STRATEGIES: dict[str, Strategy] = {
    s.slug: s for s in (MOMENTUM, VALUE, CONTRARIAN, SENTIMENT)
}


def get_strategy(slug: str) -> Strategy:
    try:
        return STRATEGIES[slug]
    except KeyError as exc:
        raise KeyError(f"unknown strategy {slug!r}; known: {sorted(STRATEGIES)}") from exc
