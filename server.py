import os
import json
import httpx
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


async def tabdeal_get(path: str, params: dict | None = None):
    url = f"{TABDEAL_BASE}{path}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def tabdeal_exchange_info() -> str:
    """
    Get active Tabdeal spot markets from the official public exchangeInfo endpoint.
    """
    data = await tabdeal_get("/r/api/v1/exchangeInfo")

    # Return a compact but useful representation.
    if isinstance(data, dict):
        symbols = data.get("symbols", data.get("data", data))

        if isinstance(symbols, list):
            active = []

            for item in symbols:
                if not isinstance(item, dict):
                    continue

                status = str(
                    item.get("status", item.get("state", ""))
                ).upper()

                if status in ("TRADING", "ACTIVE", ""):
                    active.append(item)

            return json.dumps(
                {
                    "source": "Tabdeal public API",
                    "active_market_count": len(active),
                    "markets": active,
                },
                ensure_ascii=False,
            )

    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
async def tabdeal_depth(symbol: str, limit: int = 100) -> str:
    """
    Get real-time public Tabdeal spot order-book depth.

    symbol example: ARXUSDT
    limit: maximum requested levels, normally 50-100.
    """
    symbol = symbol.upper().strip()
    limit = max(1, min(int(limit), 5000))

    data = await tabdeal_get(
        "/r/api/v1/depth",
        {"symbol": symbol, "limit": limit},
    )

    return json.dumps(
        {
            "source": "Tabdeal public API",
            "symbol": symbol,
            "limit": limit,
            "depth": data,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def tabdeal_trades(symbol: str, limit: int = 100) -> str:
    """
    Get the latest real public Tabdeal spot trades.

    symbol example: ARXUSDT
    """
    symbol = symbol.upper().strip()
    limit = max(1, min(int(limit), 1000))

    data = await tabdeal_get(
        "/r/api/v1/trades",
        {"symbol": symbol, "limit": limit},
    )

    return json.dumps(
        {
            "source": "Tabdeal public API",
            "symbol": symbol,
            "limit": limit,
            "trades": data,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def tabdeal_market_snapshot(symbol: str) -> str:
    """
    Get a combined live snapshot for one Tabdeal market:
    order book + latest trades.

    This is read-only and uses public Tabdeal endpoints.
    """
    symbol = symbol.upper().strip()

    depth = await tabdeal_get(
        "/r/api/v1/depth",
        {"symbol": symbol, "limit": 100},
    )

    trades = await tabdeal_get(
        "/r/api/v1/trades",
        {"symbol": symbol, "limit": 100},
    )

    return json.dumps(
        {
            "source": "Tabdeal public API",
            "symbol": symbol,
            "depth": depth,
            "trades": trades,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def tabdeal_scan_markets(
    symbols: str = "ARXUSDT,BTCUSDT,ETHUSDT,SOLUSDT",
) -> str:
    """
    Scan multiple Tabdeal markets using real public order-book and trade data.

    symbols should be a comma-separated list.
    Example: ARXUSDT,BTCUSDT,ETHUSDT,SOLUSDT
    """
    requested = [
        s.strip().upper()
        for s in symbols.split(",")
        if s.strip()
    ]

    # Safety limit so one MCP call does not overload the public API.
    requested = requested[:30]

    results = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for symbol in requested:
            try:
                depth_response = await client.get(
                    f"{TABDEAL_BASE}/r/api/v1/depth",
                    params={"symbol": symbol, "limit": 50},
                )
                trades_response = await client.get(
                    f"{TABDEAL_BASE}/r/api/v1/trades",
                    params={"symbol": symbol, "limit": 50},
                )

                depth_response.raise_for_status()
                trades_response.raise_for_status()

                results.append(
                    {
                        "symbol": symbol,
                        "depth": depth_response.json(),
                        "trades": trades_response.json(),
                    }
                )

            except Exception as exc:
                results.append(
                    {
                        "symbol": symbol,
                        "error": str(exc),
                    }
                )

    return json.dumps(
        {
            "source": "Tabdeal public API",
            "market_count": len(results),
            "markets": results,
        },
        ensure_ascii=False,
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse

    return JSONResponse(
        {
            "status": "ok",
            "service": "tabdeal-mcp",
            "mode": "read-only",
            "source": "Tabdeal public API",
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))

    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        json_response=True,
    )
