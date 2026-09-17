import asyncio
import json
import os
import time
from pathlib import Path

from live_scanner import LiveScanner
from target_engine import build_targets

OUT = Path("data/latest.json")
MAX_WAIT_SECONDS = int(os.getenv("SNAPSHOT_WAIT_SECONDS", "35"))


async def collect() -> None:
    scanner = LiveScanner()
    task = asyncio.create_task(scanner.start())
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    try:
        while time.monotonic() < deadline:
            snapshot = await scanner.snapshot(limit=1000)
            if snapshot.get("markets_with_live_book", 0) > 0:
                break
            await asyncio.sleep(2)

        snapshot = await scanner.snapshot(limit=1000)
        targets = build_targets(snapshot.get("markets", []), limit=60)
        payload = {
            "schema_version": 1,
            "generated_at_ms": snapshot.get("generated_at_ms"),
            "generated_at_iso": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "source": "Tabdeal public market WebSocket + public REST",
            "read_only": True,
            "market_count": snapshot.get("market_count", 0),
            "markets_with_live_book": snapshot.get("markets_with_live_book", 0),
            "connected": snapshot.get("connected", False),
            "last_message_at_ms": snapshot.get("last_message_at_ms"),
            "last_error": snapshot.get("last_error"),
            "markets": snapshot.get("markets", []),
            "targets": targets,
            "target_count": len(targets),
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

        if not payload["connected"] or payload["markets_with_live_book"] == 0:
            raise RuntimeError(
                f"No live Tabdeal order books collected: connected={payload['connected']} "
                f"live={payload['markets_with_live_book']} error={payload['last_error']}"
            )
        print(json.dumps({k: payload[k] for k in ("generated_at_ms", "market_count", "markets_with_live_book", "target_count", "connected")}, ensure_ascii=False))
    finally:
        await scanner.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(collect())
