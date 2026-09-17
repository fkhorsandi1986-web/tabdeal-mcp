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
from target_engine import build_targets

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
            "trade_flow_confirmation": True,
            "analytical_targets": True,
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
            "Recent trades are available from Tabdeal's public REST endpoint.",
            "Targets combine order-book imbalance, shift, spread quality, and recent public trade flow.",
            "Targets are transparent analytical levels, not guaranteed predictions.",
        ],
    })


async def scanner_endpoint(request: Request):
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    return JSONResponse(await scanner.snapshot(limit=limit))


async def _trade_flow(symbol: str, direction: str) -> dict | None:
    try:
        payload = await fetch_public_trades(symbol, limit=30)
        trades = payload.get("trades", [])
        if not isinstance(trades, list) or len(trades) < 3:
            return None

        buy_value = 0.0
        sell_value = 0.0
        total_value = 0.0
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            try:
                price = float(trade.get("price", 0))
                qty = float(trade.get("qty", 0))
                quote = float(trade.get("quoteQty", price * qty))
            except (TypeError, ValueError):
                continue
            if quote <= 0:
                continue
            # Tabdeal follows the Binance-style isBuyerMaker convention:
            # false means the buyer was the taker; true means the seller was the taker.
            if bool(trade.get("isBuyerMaker", False)):
                sell_value += quote
            else:
                buy_value += quote
            total_value += quote

        if total_value <= 0:
            return None

        buy_ratio = buy_value / total_value
        flow_ratio = buy_ratio if direction == "LONG" else 1.0 - buy_ratio
        return {
            "trade_count": len(trades),
            "trade_quote_volume": round(total_value, 8),
            "taker_buy_ratio": round(buy_ratio, 4),
            "directional_flow_ratio": round(flow_ratio, 4),
        }
    except Exception as exc:
        logger.debug("Trade-flow enrichment failed for %s: %s", symbol, exc)
        return None


async def _enrich_targets(targets: list[dict]) -> list[dict]:
    semaphore = asyncio.Semaphore(8)

    async def one(target: dict) -> dict | None:
        async with semaphore:
            flow = await _trade_flow(target["symbol"], target["direction"])
        if not flow:
            return None

        # Require the recent taker flow to agree with the order-book direction.
        if flow["directional_flow_ratio"] < 0.52:
            return None

        target = dict(target)
        target["trade_flow"] = flow
        target["reasons"] = list(target.get("reasons", [])) + [
            "recent public trade flow confirms direction"
        ]
        target["signal_strength"] = round(
            min(100.0, target["signal_strength"] * 0.75 + flow["directional_flow_ratio"] * 25.0),
            2,
        )
        target["method"] = "order-book imbalance + shift + spread + recent public trade flow"
        return target

    enriched = await asyncio.gather(*(one(target) for target in targets))
    results = [item for item in enriched if item is not None]
    results.sort(key=lambda x: x["signal_strength"], reverse=True)
    return results


async def targets_endpoint(request: Request):
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        limit = 20

    snapshot = await scanner.snapshot(limit=1000)
    # First shortlist by live order-book quality, then confirm only the best
    # candidates with recent public trades to avoid hammering the REST API.
    candidates = build_targets(snapshot["markets"], limit=60)
    targets = await _enrich_targets(candidates)
    targets = targets[: max(1, min(limit, 100))]

    return JSONResponse({
        "source": "Tabdeal public market WebSocket + public trades REST",
        "generated_at_ms": snapshot["generated_at_ms"],
        "market_count": snapshot["market_count"],
        "live_market_count": snapshot["markets_with_live_book"],
        "candidate_count_before_trade_confirmation": len(candidates),
        "targets": targets,
        "method": "order-book imbalance + shift + spread + recent public trade flow",
        "not_a_prediction": True,
    })


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
    Route("/api/targets", targets_endpoint, methods=["GET"]),
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
