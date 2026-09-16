
import asyncio
import json
import os
import statistics
import time
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets

WS_URL = "wss://api1.tabdeal.org/stream/"
SYMBOLS = [s.strip().lower() for s in os.getenv("SYMBOLS", "arxusdt,btcusdt").split(",") if s.strip()]
DEPTH_LEVELS = int(os.getenv("DEPTH_LEVELS", "20"))
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "300"))
IMBALANCE_ALERT = float(os.getenv("IMBALANCE_ALERT", "0.60"))

# Telegram is optional in v1. Put these in Render Environment Variables later.
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

last_alert = defaultdict(float)
history = defaultdict(lambda: deque(maxlen=20))

def now_iran():
    return datetime.now(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M:%S")

async def telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("[TELEGRAM DISABLED]", text)
        return
    import urllib.request
    import urllib.parse
    data = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text}).encode()
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        await asyncio.to_thread(urllib.request.urlopen, url, data, 15)
    except Exception as e:
        print("Telegram error:", e)

def analyze(symbol, data):
    bids = data.get("b", [])
    asks = data.get("a", [])
    if not bids or not asks:
        return None

    bids = [(float(p), float(q)) for p, q in bids[:DEPTH_LEVELS]]
    asks = [(float(p), float(q)) for p, q in asks[:DEPTH_LEVELS]]

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread_pct = (best_ask - best_bid) / mid * 100 if mid else 0

    bid_value = sum(p*q for p, q in bids)
    ask_value = sum(p*q for p, q in asks)
    imbalance = (bid_value - ask_value) / (bid_value + ask_value) if (bid_value + ask_value) else 0

    largest_bid = max(bids, key=lambda x: x[1])
    largest_ask = max(asks, key=lambda x: x[1])

    history[symbol].append(imbalance)
    prev = history[symbol][-2] if len(history[symbol]) >= 2 else imbalance
    shift = imbalance - prev

    return {
        "symbol": symbol.upper(),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_value": bid_value,
        "ask_value": ask_value,
        "imbalance": imbalance,
        "shift": shift,
        "largest_bid": largest_bid,
        "largest_ask": largest_ask,
    }

def format_report(x):
    pressure = "خریدار" if x["imbalance"] > 0.15 else "فروشنده" if x["imbalance"] < -0.15 else "خنثی"
    return (
        f"📡 {x['symbol']} | {now_iran()}\n"
        f"قیمت میانی: {x['mid']:.8g}\n"
        f"Bid: {x['best_bid']:.8g} | Ask: {x['best_ask']:.8g}\n"
        f"Spread: {x['spread_pct']:.3f}%\n"
        f"قدرت اردربوک: {pressure} | imbalance={x['imbalance']:+.2f}\n"
        f"بزرگ‌ترین سفارش خریدِ قابل‌مشاهده: {x['largest_bid'][1]:.8g} @ {x['largest_bid'][0]:.8g}\n"
        f"بزرگ‌ترین سفارش فروشِ قابل‌مشاهده: {x['largest_ask'][1]:.8g} @ {x['largest_ask'][0]:.8g}"
    )

async def maybe_alert(x):
    t = time.time()
    symbol = x["symbol"]
    strong = abs(x["imbalance"]) >= IMBALANCE_ALERT
    sharp = abs(x["shift"]) >= 0.25
    if not (strong or sharp):
        return

    if t - last_alert[symbol] < ALERT_COOLDOWN:
        return

    if x["imbalance"] >= IMBALANCE_ALERT:
        title = "🟢 فشار خرید غیرعادی"
    elif x["imbalance"] <= -IMBALANCE_ALERT:
        title = "🔴 فشار فروش غیرعادی"
    else:
        title = "🚨 تغییر سریع اردربوک"

    last_alert[symbol] = t
    await telegram(title + "\n\n" + format_report(x))

async def connect():
    streams = [f"{s}@depth@2000ms" for s in SYMBOLS]
    sub = {"method": "SUBSCRIBE", "params": streams, "id": 1}
    async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps(sub))
        print("Connected. Subscribed:", streams)
        async for raw in ws:
            try:
                msg = json.loads(raw)
                data = msg.get("data", {})
                symbol = str(data.get("s", "")).lower()
                if symbol not in SYMBOLS:
                    continue
                x = analyze(symbol, data)
                if x:
                    print(format_report(x))
                    await maybe_alert(x)
            except Exception as e:
                print("Message error:", e)

async def main():
    print("Tabdeal monitor v1")
    print("Symbols:", SYMBOLS)
    while True:
        try:
            await connect()
        except Exception as e:
            print("WebSocket disconnected:", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
