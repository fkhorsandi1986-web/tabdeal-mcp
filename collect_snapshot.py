import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from live_scanner import LiveScanner, fetch_public_trades
from target_engine import build_targets

OUT = Path("data/latest.json")
MAX_WAIT_SECONDS = 35
MIN_COLLECTION_SECONDS = 22
TRADE_FLOW_CANDIDATES = 100
TRADE_FLOW_CONCURRENCY = 10


async def trade_flow(symbol: str, direction: str) -> dict | None:
    try:
        payload = await fetch_public_trades(symbol, limit=30)
        trades = payload.get("trades", [])
        if not isinstance(trades, list) or len(trades) < 3:
            return None

        buy_value = 0.0
        sell_value = 0.0
        total_value = 0.0
        valid_count = 0
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
            if bool(trade.get("isBuyerMaker", False)):
                sell_value += quote
            else:
                buy_value += quote
            total_value += quote
            valid_count += 1

        if total_value <= 0 or valid_count < 3:
            return None

        buy_ratio = buy_value / total_value
        flow_ratio = buy_ratio if direction == "LONG" else 1.0 - buy_ratio
        return {
            "trade_count": valid_count,
            "trade_quote_volume": round(total_value, 8),
            "taker_buy_ratio": round(buy_ratio, 4),
            "directional_flow_ratio": round(flow_ratio, 4),
        }
    except Exception:
        return None


def classify(target: dict, flow: dict) -> tuple[str, list[str]] | None:
    spread = float(target.get("spread_pct", 99.0))
    imbalance = abs(float(target.get("imbalance", 0.0)))
    shift = abs(float(target.get("shift", 0.0)))
    flow_ratio = float(flow.get("directional_flow_ratio", 0.0))

    if spread > 1.20 or flow_ratio < 0.52:
        return None
    if imbalance >= 0.45 and flow_ratio >= 0.65 and spread <= 0.70 and (shift >= 0.005 or imbalance >= 0.65):
        return "A_STRONG", [
            "A: strong order-book imbalance",
            "A: agreeing recent taker flow",
            "A: acceptable spread",
        ]
    if imbalance >= 0.20 and flow_ratio >= 0.52 and spread <= 1.20:
        return "B_WATCH", [
            "B: order-book direction confirmed",
            "B: recent taker flow agrees",
            "B: requires further confirmation before A",
        ]
    return None


async def enrich_targets(candidates: list[dict]) -> tuple[list[dict], dict[str, int]]:
    semaphore = asyncio.Semaphore(TRADE_FLOW_CONCURRENCY)
    counts = {"A_STRONG": 0, "B_WATCH": 0, "rejected": 0}

    async def one(target: dict) -> dict | None:
        async with semaphore:
            flow = await trade_flow(target["symbol"], target["direction"])
        if not flow:
            counts["rejected"] += 1
            return None
        classification = classify(target, flow)
        if classification is None:
            counts["rejected"] += 1
            return None
        tier, reasons = classification
        enriched = dict(target)
        enriched["signal_tier"] = tier
        enriched["trade_flow"] = flow
        enriched["reasons"] = list(enriched.get("reasons", [])) + reasons
        enriched["signal_strength"] = round(
            min(100.0, enriched["signal_strength"] * 0.75 + flow["directional_flow_ratio"] * 25.0),
            2,
        )
        enriched["method"] = (
            "live order-book + depth quality + persistence + price confirmation + spread + wall-risk "
            "+ recent public trade flow + signal tier"
        )
        counts[tier] += 1
        return enriched

    results = await asyncio.gather(*(one(candidate) for candidate in candidates))
    results = [item for item in results if item is not None]
    tier_order = {"A_STRONG": 0, "B_WATCH": 1}
    results.sort(key=lambda x: (
        tier_order.get(x["signal_tier"], 9),
        -x["signal_strength"],
        -x.get("persistence", 0.0),
        x["spread_pct"],
        x["live_age_seconds"],
    ))
    return results, counts


async def collect() -> None:
    scanner = LiveScanner()
    task = asyncio.create_task(scanner.start())
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    collection_started = time.monotonic()
    try:
        while time.monotonic() < deadline:
            snapshot = await scanner.snapshot(limit=1000)
            if snapshot.get("markets_with_live_book", 0) > 0 and time.monotonic() - collection_started >= MIN_COLLECTION_SECONDS:
                break
            await asyncio.sleep(2)

        snapshot = await scanner.snapshot(limit=1000)
        candidates = build_targets(snapshot.get("markets", []), limit=TRADE_FLOW_CANDIDATES)
        targets, tier_counts = await enrich_targets(candidates)
        targets = targets[:60]

        payload = {
            "schema_version": 2,
            "generated_at_ms": snapshot.get("generated_at_ms"),
            "generated_at_iso": datetime.now(timezone.utc).isoformat(),
            "source": "Tabdeal public market WebSocket + public trades REST",
            "read_only": True,
            "market_count": snapshot.get("market_count", 0),
            "markets_with_live_book": snapshot.get("markets_with_live_book", 0),
            "connected": snapshot.get("connected", False),
            "last_message_at_ms": snapshot.get("last_message_at_ms"),
            "last_error": snapshot.get("last_error"),
            "markets": snapshot.get("markets", []),
            "targets": targets,
            "target_count": len(targets),
            "candidate_count_before_trade_confirmation": len(candidates),
            "signal_tier_counts": tier_counts,
            "analysis_method": "order-book + depth quality + persistence + price confirmation + spread + wall-risk + recent public trade flow",
            "collection_seconds": round(time.monotonic() - collection_started, 2),
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        if not payload["connected"] or payload["markets_with_live_book"] == 0:
            raise RuntimeError(
                f"No live Tabdeal order books collected: connected={payload['connected']} "
                f"live={payload['markets_with_live_book']} error={payload['last_error']}"
            )
        print(json.dumps({k: payload[k] for k in (
            "generated_at_ms", "market_count", "markets_with_live_book", "target_count",
            "candidate_count_before_trade_confirmation", "signal_tier_counts", "connected",
            "collection_seconds",
        )}, ensure_ascii=False))
    finally:
        await scanner.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(collect())
