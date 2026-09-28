"""Route aggregation.

One router per concern, all mounted under `settings.api_prefix` by `main`.
"""

from fastapi import APIRouter

from app.api.routes import agents, market, stream, system, trading

api_router = APIRouter()
api_router.include_router(agents.router)
api_router.include_router(trading.router)
api_router.include_router(market.router)
api_router.include_router(system.router)
api_router.include_router(stream.router)

__all__ = ["api_router"]
