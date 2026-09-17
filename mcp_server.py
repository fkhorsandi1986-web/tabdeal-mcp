from contextlib import asynccontextmanager, AsyncExitStack

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

import app as rest

mcp = FastMCP(
    "Tabdeal Live Scanner",
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
async def market_scan(limit: int = 20) -> dict:
    """Scan Tabdeal live public markets and return confirmed A/B candidates."""
    snapshot = await rest.scanner.snapshot(limit=1000)
    candidates = rest.build_targets(snapshot["markets"], limit=60)
    targets, tier_counts = await rest._enrich_targets(candidates)
    limit = max(1, min(int(limit), 100))
    return {
        "source": "Tabdeal public live market data",
        "generated_at_ms": snapshot["generated_at_ms"],
        "market_count": snapshot["market_count"],
        "live_market_count": snapshot["markets_with_live_book"],
        "candidate_count_before_trade_confirmation": len(candidates),
        "signal_tier_counts": tier_counts,
        "targets": targets[:limit],
        "not_a_prediction": True,
    }


@mcp.tool()
async def analyze_coin(symbol: str) -> dict:
    """Deep-analyze one Tabdeal symbol using its current order book and recent public trades."""
    symbol = symbol.upper().replace("_", "").replace("/", "")
    snapshot = await rest.scanner.snapshot(limit=1000)
    item = next(
        (row for row in snapshot["markets"] if str(row.get("symbol", "")).upper() == symbol),
        None,
    )
    if item is None:
        return {
            "symbol": symbol,
            "available": False,
            "reason": "No live order-book data for this symbol.",
            "market_count": snapshot["market_count"],
        }

    target = rest.build_target(item)
    flow = None
    classification = None
    if target:
        flow = await rest._trade_flow(symbol, target["direction"])
        if flow:
            classification = rest._classify_signal(target, flow)
            if classification:
                tier, reasons = classification
                target = dict(target)
                target["signal_tier"] = tier
                target["trade_flow"] = flow
                target["reasons"] = list(target.get("reasons", [])) + reasons
                target["signal_strength"] = round(
                    min(100.0, target["signal_strength"] * 0.75 + flow["directional_flow_ratio"] * 25.0),
                    2,
                )

    return {
        "source": "Tabdeal live public market data",
        "generated_at_ms": snapshot["generated_at_ms"],
        "symbol": symbol,
        "available": True,
        "market": item,
        "target": target,
        "trade_flow": flow,
        "signal_tier": classification[0] if classification else None,
        "actionability": (
            "confirmed candidate" if classification and classification[0] == "A_STRONG"
            else "watch candidate" if classification and classification[0] == "B_WATCH"
            else "no confirmed setup"
        ),
        "not_a_prediction": True,
    }


@mcp.tool()
async def scanner_status() -> dict:
    """Return the current health and live-data status of the Tabdeal scanner."""
    return await rest.scanner.snapshot(limit=1)


security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp_app = mcp.streamable_http_app(transport_security=security)


@asynccontextmanager
async def lifespan(host_app: Starlette):
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(rest.app.router.lifespan_context(rest.app))
        await stack.enter_async_context(mcp.session_manager.run())
        yield


app = Starlette(
    routes=[
        Mount("/mcp", app=mcp_app),
        Mount("/", app=rest.app),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
