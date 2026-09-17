import asyncio
import json
import os
import httpx
from starlette.responses import JSONResponse
from mcp.server import MCPServer

TABDEAL_BASE = "https://api1.tabdeal.org"

mcp = MCPServer(
    "Tabdeal Market Scanner",
    instructions=(
        "Read-only Tabdeal market data server. "
        "Uses only Tabdeal public market APIs. "
        "Never performs trading, withdrawals, account actions, or private API operations."
    ),
)

_live_scanner = None
_live_task = None


async def _ensure_live_scanner():
    """Start the shared WebSocket scanner once, so /api/check and the MCP tool use live data."""
    global _live_scanner, _live_task
    if _live_scanner is None:
        from live_scanner import LiveScanner
        _live_scanner = LiveScanner()
    if _live_task is None or _live_task.done():
        _live_task = asyncio.create_task(_live_scanner.start(), name="tabdeal-live-scanner")
    # Give the socket a short window to receive fresh books on first use.
    for _ in range(12):
        try:
            snap = await _live_scanner.snapshot(limit=1)
            if snap.get("markets_with_live_book", 0) > 0:
                return snap
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return await _live_scanner.snapshot(limit=1)


async def _build_live_check(limit: int = 1):
    """Scan live Tabdeal markets and return the strongest data-confirmed candidates."""
    from target_engine import build_targets
    from app import _enrich_targets

    snapshot = await _ensure_live_scanner()
    full = await _live_scanner.snapshot(limit=1000)
    candidates = build_targets(full["markets"], limit=60)
    targets, tier_counts = await _enrich_targets(candidates)
    targets = targets[:max(1, min(int(limit), 10))]

    return {
        "source": "Tabdeal live public market WebSocket + public trades REST",
        "generated_at_ms": full.get("generated_at_ms"),
        "market_count": full.get("market_count", 0),
        "live_market_count": full.get("markets_with_live_book", 0),
        "candidate_count_before_trade_confirmation": len(candidates),
        "signal_tier_counts": tier_counts,
        "targets": targets,
        "method": "order-book imbalance + shift + spread + recent public trade flow + signal tier",
        "not_a_prediction": True,
        "scanner_status": {
            "connected": full.get("connected"),
            "last_message_at_ms": full.get("last_message_at_ms"),
            "last_error": full.get("last_error"),
        },
    }


@mcp.tool()
async def tabdeal_check() -> str:
    """One-command live Tabdeal scan. Finds the strongest currently confirmed market candidates using live order books, spread, imbalance, rapid shifts, and recent public trade flow. Read-only; never trades."""
    return json.dumps(await _build_live_check(limit=3), ensure_ascii=False)


@mcp.tool()
async def tabdeal_exchange_info() -> str:
    """Get active Tabdeal spot markets from the official public exchangeInfo endpoint."""
    data = await tabdeal_get("/r/api/v1/exchangeInfo")
    if isinstance(data, dict):
        symbols = data.get("symbols", data.get("data", data))
        if isinstance(symbols, list):
            active = []
            for item in symbols:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", item.get("state", ""))).upper()
                if status in ("TRADING", "ACTIVE", ""):
                    active.append(item)
            return json.dumps({"source": "Tabdeal public API", "active_market_count": len(active), "markets": active}, ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)


async def tabdeal_get(path: str, params: dict | None = None):
    url = f"{TABDEAL_BASE}{path}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def tabdeal_depth(symbol: str, limit: int = 100) -> str:
    """Get real-time public Tabdeal spot order-book depth. Example: ARXUSDT."""
    symbol = symbol.upper().strip()
    limit = max(1, min(int(limit), 5000))
    data = await tabdeal_get("/r/api/v1/depth", {"symbol": symbol, "limit": limit})
    return json.dumps({"source": "Tabdeal public API", "symbol": symbol, "limit": limit, "depth": data}, ensure_ascii=False)


@mcp.tool()
async def tabdeal_trades(symbol: str, limit: int = 100) -> str:
    """Get the latest real public Tabdeal spot trades."""
    symbol = symbol.upper().strip()
    limit = max(1, min(int(limit), 1000))
    data = await tabdeal_get("/r/api/v1/trades", {"symbol": symbol, "limit": limit})
    return json.dumps({"source": "Tabdeal public API", "symbol": symbol, "limit": limit, "trades": data}, ensure_ascii=False)


@mcp.tool()
async def tabdeal_market_snapshot(symbol: str) -> str:
    """Get a combined live snapshot for one Tabdeal market: order book + latest trades."""
    symbol = symbol.upper().strip()
    depth = await tabdeal_get("/r/api/v1/depth", {"symbol": symbol, "limit": 100})
    trades = await tabdeal_get("/r/api/v1/trades", {"symbol": symbol, "limit": 100})
    return json.dumps({"source": "Tabdeal public API", "symbol": symbol, "depth": depth, "trades": trades}, ensure_ascii=False)


@mcp.tool()
async def tabdeal_scan_markets(symbols: str = "ARXUSDT,BTCUSDT,ETHUSDT,SOLUSDT") -> str:
    """Scan multiple Tabdeal markets using real public order-book and trade data."""
    requested = [s.strip().upper() for s in symbols.split(",") if s.strip()][:30]
    results = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        for symbol in requested:
            try:
                depth_response = await client.get(f"{TABDEAL_BASE}/r/api/v1/depth", params={"symbol": symbol, "limit": 50})
                trades_response = await client.get(f"{TABDEAL_BASE}/r/api/v1/trades", params={"symbol": symbol, "limit": 50})
                depth_response.raise_for_status()
                trades_response.raise_for_status()
                results.append({"symbol": symbol, "depth": depth_response.json(), "trades": trades_response.json()})
            except Exception as exc:
                results.append({"symbol": symbol, "error": str(exc)})
    return json.dumps({"source": "Tabdeal public API", "market_count": len(results), "markets": results}, ensure_ascii=False)


@mcp.custom_route("/api/check", methods=["GET"])
async def check_route(request):
    try:
        limit = int(request.query_params.get("limit", "3"))
    except ValueError:
        limit = 3
    try:
        return JSONResponse(await _build_live_check(limit=limit))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok", "service": "tabdeal-mcp", "mode": "read-only", "source": "Tabdeal public API", "live_check": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, json_response=True)
