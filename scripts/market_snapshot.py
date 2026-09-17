import asyncio
import json
import os
import time
from pathlib import Path

from live_scanner import LiveScanner, fetch_public_trades
from target_engine import build_targets


def classify(target, flow):
    spread = float(target.get("spread_pct", 99.0))
    imbalance = abs(float(target.get("imbalance", 0.0)))
    shift = abs(float(target.get("shift", 0.0)))
    flow_ratio = float(flow.get("directional_flow_ratio", 0.0))
    if spread > 1.20 or flow_ratio < 0.52:
        return None
    if imbalance >= 0.45 and flow_ratio >= 0.65 and spread <= 0.70 and (shift >= 0.005 or imbalance >= 0.65):
        return "A_STRONG"
    if imbalance >= 0.20 and flow_ratio >= 0.52 and spread <= 1.20:
        return "B_WATCH"
    return None


async def trade_flow(symbol, direction):
    try:
        payload = await fetch_public_trades(symbol, limit=30)
        trades = payload.get("trades", [])
        buy_value = sell_value = total = 0.0
        count = 0
        for trade in trades:
            try:
                price = float(trade.get("price", 0))
                qty = float(trade.get("qty", 0))
                quote = float(trade.get("quoteQty", price * qty))
            except (TypeError, ValueError):
                continue
            if quote <= 0:
                continue
            if bool(trade.get("isBuyerMaker", False)):
                sell_value += quote
            else:
                buy_value += quote
            total += quote
            count += 1
        if count < 3 or total <= 0:
            return None
        buy_ratio = buy_value / total
        return {
            "trade_count": count,
            "trade_quote_volume": round(total, 8),
            "taker_buy_ratio": round(buy_ratio, 4),
            "directional_flow_ratio": round(buy_ratio if direction == "LONG" else 1 - buy_ratio, 4),
        }
    except Exception:
        return None


async def main():
    scanner = LiveScanner()
    task = asyncio.create_task(scanner.start())
    try:
        deadline = time.time() + int(os.getenv("SNAPSHOT_WAIT_SECONDS", "25"))
        snapshot = {"markets": []}
        while time.time() < deadline:
            await asyncio.sleep(2)
            snapshot = await scanner.snapshot(limit=1000)
            if snapshot.get("markets_with_live_book", 0) >= 5:
                break

        candidates = build_targets(snapshot.get("markets", []), limit=60)
        enriched = []
        for target in candidates:
            flow = await trade_flow(target["symbol"], target["direction"])
            if not flow:
                continue
            tier = classify(target, flow)
            if not tier:
                continue
            item = dict(target)
            item["signal_tier"] = tier
            item["trade_flow"] = flow
            item["signal_strength"] = round(min(100.0, item["signal_strength"] * 0.75 + flow["directional_flow_ratio"] * 25.0), 2)
            enriched.append(item)

        tier_order = {"A_STRONG": 0, "B_WATCH": 1}
        enriched.sort(key=lambda x: (tier_order.get(x["signal_tier"], 9), -x["signal_strength"]))
        output = {
            "generated_at_ms": int(time.time() * 1000),
            "source": "Tabdeal public WebSocket + public trades REST",
            "connected": snapshot.get("connected"),
            "market_count": snapshot.get("market_count"),
            "live_market_count": snapshot.get("markets_with_live_book"),
            "last_error": snapshot.get("last_error"),
            "targets": enriched[:20],
            "markets": snapshot.get("markets", []),
            "read_only": True,
            "not_a_prediction": True,
        }
        path = Path("data/market_snapshot.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await scanner.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
