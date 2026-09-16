import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

TABDEAL_BASE = os.getenv("TABDEAL_BASE", "https://api1.tabdeal.org").rstrip("/")
TABDEAL_WS = os.getenv("TABDEAL_WS", "wss://api1.tabdeal.org/stream/")
DEPTH_LEVELS = max(1, min(int(os.getenv("DEPTH_LEVELS", "20")), 100))
SUBSCRIBE_BATCH = max(1, min(int(os.getenv("SUBSCRIBE_BATCH", "100")), 250))
MARKET_REFRESH_SECONDS = max(30, int(os.getenv("MARKET_REFRESH_SECONDS", "300")))
MAX_HISTORY = max(5, int(os.getenv("MAX_HISTORY", "30")))

logger = logging.getLogger("tabdeal-live-scanner")


class LiveScanner:
    """Read-only Tabdeal market collector.

    The documented public market websocket provides order-book updates.
    We keep the latest book for every active market and derive pressure,
    spread and anomaly metrics without placing any orders.
    """

    def __init__(self) -> None:
        self.symbols: list[str] = []
        self.books: dict[str, dict[str, Any]] = {}
        self.history: dict[str, deque[float]] = {}
        self.last_error: str | None = None
        self.last_market_refresh = 0.0
        self.last_message_at = 0.0
        self.connected = False
        self.started_at = time.time()
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def fetch_exchange_info(self) -> list[str]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{TABDEAL_BASE}/r/api/v1/exchangeInfo")
            response.raise_for_status()
            payload = response.json()

        items: Any = payload.get("symbols", payload.get("data", payload)) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise RuntimeError("Unexpected exchangeInfo response")

        active: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", item.get("state", ""))).upper()
            symbol = str(item.get("symbol", item.get("market", ""))).replace("_", "").upper()
            if not symbol or (status and status not in {"TRADING", "ACTIVE"}):
                continue
            active.append(symbol)

        # Stable, de-duplicated order.
        result = list(dict.fromkeys(active))
        if not result:
            raise RuntimeError("No active markets returned by exchangeInfo")
        return result

    async def refresh_markets(self) -> None:
        symbols = await self.fetch_exchange_info()
        async with self._lock:
            self.symbols = symbols
            self.last_market_refresh = time.time()
            for symbol in symbols:
                self.history.setdefault(symbol, deque(maxlen=MAX_HISTORY))
        logger.info("Loaded %d active markets", len(symbols))

    @staticmethod
    def _book_metrics(symbol: str, data: dict[str, Any], depth_levels: int) -> dict[str, Any] | None:
        bids_raw = data.get("b", data.get("bids", []))
        asks_raw = data.get("a", data.get("asks", []))
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list) or not bids_raw or not asks_raw:
            return None

        def parse(levels: list[Any]) -> list[tuple[float, float]]:
            parsed = []
            for level in levels[:depth_levels]:
                if not isinstance(level, (list, tuple)) or len(level) < 2:
                    continue
                try:
                    price = float(level[0])
                    qty = float(level[1])
                    if price > 0 and qty >= 0:
                        parsed.append((price, qty))
                except (TypeError, ValueError):
                    continue
            return parsed

        bids = parse(bids_raw)
        asks = parse(asks_raw)
        if not bids or not asks:
            return None

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2.0
        spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid else 0.0
        bid_value = sum(price * qty for price, qty in bids)
        ask_value = sum(price * qty for price, qty in asks)
        total = bid_value + ask_value
        imbalance = (bid_value - ask_value) / total if total else 0.0
        largest_bid = max(bids, key=lambda x: x[1])
        largest_ask = max(asks, key=lambda x: x[1])

        return {
            "symbol": symbol,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_pct": spread_pct,
            "bid_value": bid_value,
            "ask_value": ask_value,
            "imbalance": imbalance,
            "largest_bid": {"price": largest_bid[0], "qty": largest_bid[1]},
            "largest_ask": {"price": largest_ask[0], "qty": largest_ask[1]},
            "bid_levels": bids,
            "ask_levels": asks,
        }

    async def handle_message(self, raw: str) -> None:
        message = json.loads(raw)
        if not isinstance(message, dict):
            return
        data = message.get("data")
        if not isinstance(data, dict):
            return
        symbol = str(data.get("s", "")).upper()
        if not symbol:
            return
        metrics = self._book_metrics(symbol, data, DEPTH_LEVELS)
        if not metrics:
            return

        now = time.time()
        event_time = data.get("E")
        try:
            event_time = int(event_time) if event_time is not None else None
        except (TypeError, ValueError):
            event_time = None

        async with self._lock:
            history = self.history.setdefault(symbol, deque(maxlen=MAX_HISTORY))
            previous = history[-1] if history else metrics["imbalance"]
            shift = metrics["imbalance"] - previous
            history.append(metrics["imbalance"])
            metrics["shift"] = shift
            metrics["received_at_ms"] = int(now * 1000)
            metrics["exchange_event_at_ms"] = event_time
            metrics["age_ms"] = max(0, int(now * 1000 - event_time)) if event_time else None
            metrics["pressure"] = (
                "BUY" if metrics["imbalance"] >= 0.15 else
                "SELL" if metrics["imbalance"] <= -0.15 else "NEUTRAL"
            )
            metrics["anomaly"] = bool(abs(metrics["imbalance"]) >= 0.60 or abs(shift) >= 0.25)
            self.books[symbol] = metrics
            self.last_message_at = now

    async def websocket_loop(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            try:
                if not self.symbols or time.time() - self.last_market_refresh >= MARKET_REFRESH_SECONDS:
                    await self.refresh_markets()

                streams = [f"{symbol.lower()}@depth@2000ms" for symbol in self.symbols]
                async with websockets.connect(
                    TABDEAL_WS,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    self.connected = True
                    self.last_error = None
                    backoff = 2

                    for start in range(0, len(streams), SUBSCRIBE_BATCH):
                        batch = streams[start:start + SUBSCRIBE_BATCH]
                        request = {"method": "SUBSCRIBE", "params": batch, "id": start // SUBSCRIBE_BATCH + 1}
                        await ws.send(json.dumps(request))

                    logger.info("WebSocket connected: %d subscriptions", len(streams))
                    async for raw in ws:
                        await self.handle_message(raw)
                        if self._stop.is_set():
                            break

            except (ConnectionClosed, OSError, asyncio.TimeoutError, httpx.HTTPError, RuntimeError) as exc:
                self.last_error = str(exc)
                logger.warning("WebSocket loop error: %s", exc)
            except Exception as exc:  # Defensive: scanner must stay alive.
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Unexpected scanner error")
            finally:
                self.connected = False

            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def start(self) -> None:
        await self.refresh_markets()
        await self.websocket_loop()

    async def stop(self) -> None:
        self._stop.set()

    async def snapshot(self, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1000))
        async with self._lock:
            rows = []
            for symbol, item in self.books.items():
                row = {k: v for k, v in item.items() if k not in {"bid_levels", "ask_levels"}}
                rows.append(row)
            rows.sort(key=lambda x: (abs(float(x.get("imbalance", 0))), -float(x.get("spread_pct", 0))), reverse=True)
            return {
                "source": "Tabdeal public market WebSocket",
                "generated_at_ms": int(time.time() * 1000),
                "connected": self.connected,
                "market_count": len(self.symbols),
                "markets_with_live_book": len(rows),
                "last_message_at_ms": int(self.last_message_at * 1000) if self.last_message_at else None,
                "last_error": self.last_error,
                "markets": rows[:limit],
            }

    async def orderbook(self, symbol: str, levels: int = 20) -> dict[str, Any]:
        symbol = symbol.upper().replace("_", "")
        levels = max(1, min(int(levels), DEPTH_LEVELS))
        async with self._lock:
            item = self.books.get(symbol)
            if not item:
                return {"symbol": symbol, "available": False, "reason": "No live order-book update received yet"}
            return {
                "source": "Tabdeal public market WebSocket",
                "symbol": symbol,
                "available": True,
                "generated_at_ms": int(time.time() * 1000),
                "best_bid": item["best_bid"],
                "best_ask": item["best_ask"],
                "mid": item["mid"],
                "spread_pct": item["spread_pct"],
                "imbalance": item["imbalance"],
                "pressure": item["pressure"],
                "shift": item["shift"],
                "bids": item["bid_levels"][:levels],
                "asks": item["ask_levels"][:levels],
                "received_at_ms": item["received_at_ms"],
                "exchange_event_at_ms": item["exchange_event_at_ms"],
            }

    async def market_list(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "source": "Tabdeal public API exchangeInfo",
                "generated_at_ms": int(time.time() * 1000),
                "count": len(self.symbols),
                "symbols": self.symbols,
            }


async def fetch_public_trades(symbol: str, limit: int = 100) -> dict[str, Any]:
    symbol = symbol.upper().replace("_", "")
    limit = max(1, min(int(limit), 1000))
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f"{TABDEAL_BASE}/r/api/v1/trades",
            params={"symbol": symbol, "limit": limit},
        )
        response.raise_for_status()
        trades = response.json()
    return {
        "source": "Tabdeal public REST trades",
        "symbol": symbol,
        "generated_at_ms": int(time.time() * 1000),
        "limit": limit,
        "trades": trades,
    }
