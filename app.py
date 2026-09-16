import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from live_scanner import LiveScanner, fetch_public_trades

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tabdeal-api")
scanner = LiveScanner()
scanner_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: Starlette):
    global scanner_task
    scanner_task = asyncio.create_task(scanner.start(), name="tabdeal-live-scanner")
    try:
        yield
    finally:
        await scanner.stop()
        if scanner_task:
            scanner_task.cancel()
            await asyncio.gather(scanner_task, return_exceptions=True)


async def health(request: Request):
    snapshot = await scanner.snapshot(limit=1)
    return JSONResponse({
        "status": "ok",
        "service": "tabdeal-live-scanner",
        "mode": "read-only",
        "connected": snapshot["connected"],
        "market_count": snapshot["market_count"],
        "markets_with_live_book": snapshot["markets_with_live_book"],
        "last_message_at_ms": snapshot["last_message_at_ms"],
        "last_error": snapshot["last_error"],
    })


async def capabilities(request: Request):
    return JSONResponse({
        "service": "tabdeal-live-scanner",
        "read_only": True,
        "live": {
            "active_market_discovery": True,
            "orderbook_websocket": True,
            "orderbook_pressure": True,
            "spread": True,
            "imbalance": True,
            "rapid_imbalance_shift": True,
            "public_recent_trades": True,
        },
        "not_claimed_without_a_public_source": [
            "private account data",
            "private orders",
            "private leverage positions",
            "private liquidations",
            "private open interest",
            "funding data when Tabdeal does not expose it publicly",
        ],
        "notes": [
            "The scanner never places, cancels, or modifies orders.",
            "Order-book values are visible market liquidity, not proof of whale ownership.",
            "Recent trades are available on demand from Tabdeal's public REST endpoint.",
        ],
    })


async def scanner_endpoint(request: Request):
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    return JSONResponse(await scanner.snapshot(limit=limit))


async def markets_endpoint(request: Request):
    return JSONResponse(await scanner.market_list())


async def orderbook_endpoint(request: Request):
    symbol = request.path_params["symbol"]
    try:
        levels = int(request.query_params.get("levels", "20"))
    except ValueError:
        levels = 20
    return JSONResponse(await scanner.orderbook(symbol, levels=levels))


async def trades_endpoint(request: Request):
    symbol = request.path_params["symbol"]
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    try:
        return JSONResponse(await fetch_public_trades(symbol, limit=limit))
    except Exception as exc:
        logger.warning("Trades request failed for %s: %s", symbol, exc)
        return JSONResponse({
            "source": "Tabdeal public REST trades",
            "symbol": symbol.upper(),
            "error": str(exc),
        }, status_code=502)


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/api/capabilities", capabilities, methods=["GET"]),
    Route("/api/scanner", scanner_endpoint, methods=["GET"]),
    Route("/api/markets", markets_endpoint, methods=["GET"]),
    Route("/api/orderbook/{symbol}", orderbook_endpoint, methods=["GET"]),
    Route("/api/trades/{symbol}", trades_endpoint, methods=["GET"]),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level=os.getenv("LOG_LEVEL", "info").lower())
