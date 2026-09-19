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
STALE_AFTER_SECONDS = max(5, int(os.getenv("STALE_AFTER_SECONDS", "20")))
MAX_HISTORY = max(15, int(os.getenv("MAX_HISTORY", "60")))

logger = logging.getLogger("tabdeal-live-scanner")


class LiveScanner:
    """Read-only Tabdeal market collector with multi-window microstructure features."""

    def __init__(self) -> None:
        self.symbols: list[str] = []
        self.books: dict[str, dict[str, Any]] = {}
        self.history: dict[str, deque[dict[str, float]]] = {}
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

        result = list(dict.fromkeys(active))
        if not result:
            raise RuntimeError("No active markets returned by exchangeInfo")
        return result

    async def refresh_markets(self) -> bool:
        symbols = await self.fetch_exchange_info()
        async with self._lock:
            old = set(self.symbols)
            new = set(symbols)
            changed = old != new
            self.symbols = symbols
            self.last_market_refresh = time.time()
            for symbol in symbols:
                self.history.setdefault(symbol, deque(maxlen=MAX_HISTORY))
            for symbol in old - new:
                self.books.pop(symbol, None)
                self.history.pop(symbol, None)
        logger.info("Loaded %d active markets (changed=%s)", len(symbols), changed)
        return changed

    @staticmethod
    def _book_metrics(symbol: str, data: dict[str, Any], depth_levels: int) -> dict[str, Any] | None:
        bids_raw = data.get("b", data.get("bids", []))
        asks_raw = data.get("a", data.get("asks", []))
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list) or not bids_raw or not asks_raw:
            return None

        def parse(levels: list[Any]) -> list[tuple[float, float]]:
            parsed: list[tuple[float, float]] = []
            for level in levels[:depth_levels]:
                if not isinstance(level, (list, tuple)) or len(level) < 2:
                    continue
                try:
                    price, qty = float(level[0]), float(level[1])
                    if price > 0 and qty >= 0:
                        parsed.append((price, qty))
                except (TypeError, ValueError):
                    continue
            return parsed

        bids, asks = parse(bids_raw), parse(asks_raw)
        if not bids or not asks:
            return None

        best_bid, best_ask = bids[0][0], asks[0][0]
        mid = (best_bid + best_ask) / 2.0
        spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid else 0.0
        bid_value = sum(p * q for p, q in bids)
        ask_value = sum(p * q for p, q in asks)
        total = bid_value + ask_value
        imbalance = (bid_value - ask_value) / total if total else 0.0

        largest_bid = max(bids, key=lambda x: x[0] * x[1])
        largest_ask = max(asks, key=lambda x: x[0] * x[1])
        largest_bid_value = largest_bid[0] * largest_bid[1]
        largest_ask_value = largest_ask[0] * largest_ask[1]

        # Near-touch depth is harder to manipulate with one distant wall.
        near = max(1, min(5, depth_levels))
        bid_near = sum(p * q for p, q in bids[:near])
        ask_near = sum(p * q for p, q in asks[:near])
        near_total = bid_near + ask_near
        near_imbalance = (bid_near - ask_near) / near_total if near_total else 0.0

        return {
            "symbol": symbol, "best_bid": best_bid, "best_ask": best_ask, "mid": mid,
            "spread_pct": spread_pct, "bid_value": bid_value, "ask_value": ask_value,
            "total_depth_value": total, "imbalance": imbalance, "near_imbalance": near_imbalance,
            "largest_bid": {"price": largest_bid[0], "qty": largest_bid[1]},
            "largest_ask": {"price": largest_ask[0], "qty": largest_ask[1]},
            "largest_bid_share": largest_bid_value / bid_value if bid_value else 0.0,
            "largest_ask_share": largest_ask_value / ask_value if ask_value else 0.0,
            "bid_levels": bids, "ask_levels": asks,
        }

    async def handle_message(self, raw: str) -> None:
        message = json.loads(raw)
        if not isinstance(message, dict) or not isinstance(message.get("data"), dict):
            return
        data = message["data"]
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
            previous = history[-1] if history else None
            previous_imbalance = previous["imbalance"] if previous else metrics["imbalance"]
            previous_mid = previous["mid"] if previous else metrics["mid"]
            shift = metrics["imbalance"] - previous_imbalance
            price_change_pct = ((metrics["mid"] - previous_mid) / previous_mid * 100.0) if previous_mid else 0.0

            history.append({
                "imbalance": metrics["imbalance"], "near_imbalance": metrics["near_imbalance"],
                "mid": metrics["mid"], "largest_bid_share": metrics["largest_bid_share"],
                "largest_ask_share": metrics["largest_ask_share"],
            })

            recent = list(history)
            window10 = recent[-10:]
            window30 = recent[-30:]

            def persistence(rows: list[dict[str, float]]) -> float:
                if not rows:
                    return 0.0
                sign = 1 if metrics["imbalance"] >= 0.15 else -1 if metrics["imbalance"] <= -0.15 else 0
                if sign == 0:
                    return 0.0
                return sum(1 for row in rows if (row["imbalance"] >= 0.15 if sign > 0 else row["imbalance"] <= -0.15)) / len(rows)

            persistence10 = persistence(window10)
            persistence30 = persistence(window30)

            price_5 = recent[-6]["mid"] if len(recent) >= 6 else previous_mid
            price_15 = recent[-16]["mid"] if len(recent) >= 16 else previous_mid
            momentum_5 = ((metrics["mid"] - price_5) / price_5 * 100.0) if price_5 else 0.0
            momentum_15 = ((metrics["mid"] - price_15) / price_15 * 100.0) if price_15 else 0.0

            imbalance_values = [r["imbalance"] for r in window10]
            avg_imbalance = sum(imbalance_values) / len(imbalance_values) if imbalance_values else 0.0
            imbalance_volatility = (
                sum((x - avg_imbalance) ** 2 for x in imbalance_values) / len(imbalance_values)
            ) ** 0.5 if imbalance_values else 0.0

            if metrics["imbalance"] >= 0.15:
                opposite_wall_share = metrics["largest_ask_share"]
            elif metrics["imbalance"] <= -0.15:
                opposite_wall_share = metrics["largest_bid_share"]
            else:
                opposite_wall_share = max(metrics["largest_bid_share"], metrics["largest_ask_share"])

            metrics.update({
                "shift": shift, "price_change_pct": price_change_pct,
                "momentum_5_pct": momentum_5, "momentum_15_pct": momentum_15,
                "persistence": persistence10, "persistence_30": persistence30,
                "imbalance_volatility": imbalance_volatility,
                "history_samples": len(history), "opposite_wall_share": opposite_wall_share,
                "received_at_ms": int(now * 1000), "exchange_event_at_ms": event_time,
                "age_ms": max(0, int(now * 1000 - event_time)) if event_time else None,
                "pressure": "BUY" if metrics["imbalance"] >= 0.15 else "SELL" if metrics["imbalance"] <= -0.15 else "NEUTRAL",
                "anomaly": bool(abs(metrics["imbalance"]) >= 0.60 or abs(shift) >= 0.25),
            })
            self.books[symbol] = metrics
            self.last_message_at = now

    async def _subscribe(self, ws, symbols: list[str]) -> None:
        streams = [f"{symbol.lower()}@depth@2000ms" for symbol in symbols]
        for start in range(0, len(streams), SUBSCRIBE_BATCH):
            await ws.send(json.dumps({"method": "SUBSCRIBE", "params": streams[start:start + SUBSCRIBE_BATCH], "id": start // SUBSCRIBE_BATCH + 1}))
        logger.info("Subscribed to %d order-book streams", len(streams))

    async def websocket_loop(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            try:
                if not self.symbols or time.time() - self.last_market_refresh >= MARKET_REFRESH_SECONDS:
                    await self.refresh_markets()
                subscribed_symbols = list(self.symbols)
                async with websockets.connect(TABDEAL_WS, ping_interval=20, ping_timeout=20, close_timeout=10, max_size=8 * 1024 * 1024) as ws:
                    self.connected, self.last_error, backoff = True, None, 2
                    await self._subscribe(ws, subscribed_symbols)
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8", errors="replace")
                            await self.handle_message(raw)
                        except asyncio.TimeoutError:
                            if time.time() - self.last_market_refresh >= MARKET_REFRESH_SECONDS and await self.refresh_markets():
                                break
            except (ConnectionClosed, OSError, asyncio.TimeoutError, httpx.HTTPError, RuntimeError) as exc:
                self.last_error = str(exc)
                logger.warning("WebSocket loop error: %s", exc)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Unexpected scanner error")
            finally:
                self.connected = False
            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def start(self) -> None:
        await self.websocket_loop()

    async def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _fresh(item: dict[str, Any], now_ms: int) -> tuple[bool, float]:
        received = int(item.get("received_at_ms", 0) or 0)
        age_seconds = max(0.0, (now_ms - received) / 1000.0) if received else float("inf")
        return age_seconds <= STALE_AFTER_SECONDS, age_seconds

    async def snapshot(self, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1000))
        now_ms = int(time.time() * 1000)
        async with self._lock:
            rows, fresh_count = [], 0
            for item in self.books.values():
                fresh, age_seconds = self._fresh(item, now_ms)
                fresh_count += int(fresh)
                row = {k: v for k, v in item.items() if k not in {"bid_levels", "ask_levels"}}
                row["live_age_seconds"] = round(age_seconds, 3) if age_seconds != float("inf") else None
                row["stale"] = not fresh
                rows.append(row)
            rows.sort(key=lambda x: (x["stale"], -abs(float(x.get("imbalance", 0))), float(x.get("spread_pct", 0))))
            return {
                "source": "Tabdeal public market WebSocket", "generated_at_ms": now_ms,
                "connected": self.connected, "market_count": len(self.symbols),
                "markets_with_live_book": fresh_count, "markets_with_any_book": len(rows),
                "stale_after_seconds": STALE_AFTER_SECONDS,
                "last_message_at_ms": int(self.last_message_at * 1000) if self.last_message_at else None,
                "last_error": self.last_error, "markets": rows[:limit],
            }

    async def orderbook(self, symbol: str, levels: int = 20) -> dict[str, Any]:
        symbol = symbol.upper().replace("_", "")
        levels = max(1, min(int(levels), DEPTH_LEVELS))
        now_ms = int(time.time() * 1000)
        async with self._lock:
            item = self.books.get(symbol)
            if not item:
                return {"symbol": symbol, "available": False, "reason": "No live order-book update received yet"}
            fresh, age_seconds = self._fresh(item, now_ms)
            if not fresh:
                return {"source": "Tabdeal public market WebSocket", "symbol": symbol, "available": False, "stale": True,
                        "live_age_seconds": round(age_seconds, 3), "reason": f"Last order-book update is older than {STALE_AFTER_SECONDS} seconds",
                        "received_at_ms": item["received_at_ms"], "exchange_event_at_ms": item["exchange_event_at_ms"]}
            return {
                "source": "Tabdeal public market WebSocket", "symbol": symbol, "available": True, "stale": False,
                "live_age_seconds": round(age_seconds, 3), "generated_at_ms": now_ms,
                "best_bid": item["best_bid"], "best_ask": item["best_ask"], "mid": item["mid"],
                "spread_pct": item["spread_pct"], "imbalance": item["imbalance"], "near_imbalance": item["near_imbalance"],
                "pressure": item["pressure"], "shift": item["shift"], "persistence": item["persistence"],
                "persistence_30": item["persistence_30"], "momentum_5_pct": item["momentum_5_pct"],
                "momentum_15_pct": item["momentum_15_pct"], "imbalance_volatility": item["imbalance_volatility"],
                "price_change_pct": item["price_change_pct"], "bids": item["bid_levels"][:levels], "asks": item["ask_levels"][:levels],
                "received_at_ms": item["received_at_ms"], "exchange_event_at_ms": item["exchange_event_at_ms"],
            }

    async def market_list(self) -> dict[str, Any]:
        async with self._lock:
            return {"source": "Tabdeal public API exchangeInfo", "generated_at_ms": int(time.time() * 1000),
                    "count": len(self.symbols), "symbols": self.symbols}


async def fetch_public_trades(symbol: str, limit: int = 100) -> dict[str, Any]:
    symbol = symbol.upper().replace("_", "")
    limit = max(1, min(int(limit), 1000))
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(f"{TABDEAL_BASE}/r/api/v1/trades", params={"symbol": symbol, "limit": limit})
        response.raise_for_status()
        trades = response.json()
    return {"source": "Tabdeal public REST trades", "symbol": symbol, "generated_at_ms": int(time.time() * 1000),
            "limit": limit, "trades": trades}
