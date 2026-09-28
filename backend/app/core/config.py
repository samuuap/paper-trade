"""Central configuration.

Everything tunable lives here so that behaviour can be changed without touching
logic. Values come from environment variables (see `.env.example` at the repo
root); the prefixes group them by concern.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- general
    env: Literal["local", "docker", "test"] = "local"
    log_level: str = "INFO"
    log_json: bool = False
    timezone_market: str = "America/New_York"

    # --------------------------------------------------------------- database
    database_url: str = "postgresql+asyncpg://paper:paper@localhost:5432/papertrade"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    @field_validator("database_url")
    @classmethod
    def _force_async_driver(cls, v: str) -> str:
        # Alembic and some tooling hand us a sync URL; normalise it so the app
        # always talks asyncpg.
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    @property
    def sync_database_url(self) -> str:
        """Sync (psycopg) flavour of `database_url`, used by Alembic."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)

    # ----------------------------------------------------------- market data
    finnhub_api_key: str = ""
    finnhub_rest_url: str = "https://finnhub.io/api/v1"
    finnhub_ws_url: str = "wss://ws.finnhub.io"
    #: Finnhub free tier is 60 calls/minute. We stay a touch under it.
    finnhub_rate_limit_per_minute: int = 55
    #: Free tier caps the websocket at ~50 symbols.
    finnhub_ws_max_symbols: int = 45
    #: `/stock/candle` is a premium endpoint. Off by default: daily bars are
    #: assembled from the free `/quote` endpoint after the close instead.
    finnhub_candles_enabled: bool = False
    #: When true the market layer serves synthetic data instead of calling out.
    market_data_offline: bool = False

    # -------------------------------------------------------------- universe
    universe_file: str = "app/data/sp500.csv"
    #: How many top screener candidates get promoted to the live websocket feed.
    live_feed_candidate_slots: int = 20

    # --------------------------------------------------------------- trading
    initial_capital: float = 100_000.0
    slippage_bps: float = 5.0
    commission_per_share: float = 0.005
    commission_minimum: float = 1.0
    commission_maximum_pct: float = 0.005
    #: Cap on how much of a bar's volume a single order may consume; anything
    #: above it fills partially. Keeps simulated fills honest for illiquid names.
    max_participation_rate: float = 0.05
    allow_short: bool = False
    #: Fractional shares keep the Buy & Hold SPY baseline free of cash drag, so
    #: its return tracks the index exactly — which is the point of a sanity check.
    allow_fractional_shares: bool = True
    #: Risk rails applied deterministically in Python before the LLM ever sees
    #: an order. The model proposes, these dispose.
    max_position_pct: float = 0.20
    max_gross_exposure_pct: float = 1.0
    min_order_notional: float = 100.0
    max_orders_per_cycle: int = 5
    cash_buffer_pct: float = 0.02

    # ------------------------------------------------------------- inference
    llm_base_url: str = "http://host.docker.internal:8080/v1"
    llm_api_key: str = "sk-no-key-required"
    #: Model served during market hours (small/fast, leaves the laptop usable).
    llm_model_open: str = "qwen3-14b-instruct-q4_k_m"
    #: Larger MoE loaded overnight/weekends for deep research.
    llm_model_closed: str = "qwen3-30b-a3b-instruct-q4_k_m"
    llm_timeout_seconds: float = 300.0
    llm_temperature: float = 0.4
    llm_top_p: float = 0.9
    llm_max_tokens: int = 1536
    llm_max_tool_iterations: int = 4
    llm_max_retries: int = 3
    #: `grammar` uses GBNF (strongest guarantee, llama.cpp only),
    #: `json_schema` uses the OpenAI-style response_format,
    #: `none` trusts the model (only useful for benchmarking the difference).
    structured_mode: Literal["grammar", "json_schema", "none"] = "grammar"
    #: Expose `place_order` as a model-invocable tool. Off by default: orders
    #: travel through the final structured decision so they can be validated
    #: and sized deterministically before touching the ledger.
    expose_place_order_tool: bool = False
    #: Process name of the local inference server. The resource guard matches
    #: on it to report whether the model is still resident and how much memory
    #: it holds — which is otherwise invisible from outside the server.
    inference_process_name: str = "llama-server"

    # ---------------------------------------------------------- shadow cost
    #: Price of the frontier API we benchmark local inference against, in USD
    #: per million tokens. Used for the "what would this have cost" metric.
    shadow_model_name: str = "claude-sonnet-5"
    shadow_cost_input_per_mtok: float = 3.0
    shadow_cost_output_per_mtok: float = 15.0
    #: Rough sustained draw of the machine while generating, for energy cost.
    inference_watts: float = 50.0
    electricity_cost_per_kwh: float = 0.18

    # ---------------------------------------------------------- orchestrator
    run_orchestrator: bool = True
    decision_interval_minutes: int = 15
    equity_snapshot_seconds: int = 60
    #: How often quotes for the active set are polled while the market is open.
    quote_poll_seconds: int = 60
    #: Company-news refresh cadence during PRE_MARKET and OPEN.
    news_poll_minutes: int = 30
    #: How often a RESEARCH turn runs during the long CLOSED stretch. Overnight
    #: is 12+ hours; without a cadence the agents would think once and idle.
    research_interval_hours: float = 6.0
    #: Longest the scheduler will sleep between ticks. Caps how long a shutdown
    #: or a clock correction takes to be noticed.
    scheduler_max_sleep_seconds: float = 30.0
    #: Skip the phase machine's real clock and drive it from a replay cursor.
    replay_mode: bool = False
    replay_start: Optional[str] = None
    replay_end: Optional[str] = None
    replay_speed: float = 60.0
    #: Resource guard: refuse to schedule inference when free memory dips below
    #: this, and never load the big model if it would not fit.
    min_free_memory_gb: float = 4.0
    model_memory_estimate_open_gb: float = 11.0
    model_memory_estimate_closed_gb: float = 20.0

    # ------------------------------------------------------------------- api
    api_prefix: str = "/api"
    cors_origins: str = "http://localhost:3000"
    sse_keepalive_seconds: int = 15

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
