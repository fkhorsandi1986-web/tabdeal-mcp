import asyncio
import json
import time

from live_scanner import LiveScanner


def run(coro):
    return asyncio.run(coro)


def test_book_metrics_calculates_spread_and_imbalance():
    data = {
        "b": [[100, 2], [99, 1]],
        "a": [[101, 1], [102, 1]],
    }
    result = LiveScanner._book_metrics("BTCUSDT", data, 20)

    assert result is not None
    assert result["best_bid"] == 100.0
    assert result["best_ask"] == 101.0
    assert result["mid"] == 100.5
    assert round(result["spread_pct"], 6) == round((1 / 100.5) * 100, 6)
    assert result["bid_value"] == 299.0
    assert result["ask_value"] == 203.0
    assert round(result["imbalance"], 6) == round((299 - 203) / (299 + 203), 6)


def test_book_metrics_ignores_malformed_levels():
    data = {
        "b": [[100, 2], ["bad"], ["x", "y"], [99, 1]],
        "a": [[101, 1], None, [102, 1]],
    }
    result = LiveScanner._book_metrics("BTCUSDT", data, 20)

    assert result is not None
    assert result["bid_levels"] == [(100.0, 2.0), (99.0, 1.0)]
    assert result["ask_levels"] == [(101.0, 1.0), (102.0, 1.0)]


def test_handle_message_sets_pressure_and_shift():
    scanner = LiveScanner()
    now_ms = int(time.time() * 1000)

    first = {
        "data": {
            "s": "BTCUSDT",
            "E": now_ms,
            "b": [[100, 5], [99, 5]],
            "a": [[101, 1], [102, 1]],
        }
    }
    second = {
        "data": {
            "s": "BTCUSDT",
            "E": now_ms,
            "b": [[100, 1], [99, 1]],
            "a": [[101, 5], [102, 5]],
        }
    }

    run(scanner.handle_message(json.dumps(first)))
    assert scanner.books["BTCUSDT"]["shift"] == 0.0
    assert scanner.books["BTCUSDT"]["pressure"] == "BUY"

    run(scanner.handle_message(json.dumps(second)))
    assert scanner.books["BTCUSDT"]["pressure"] == "SELL"
    assert scanner.books["BTCUSDT"]["shift"] < 0


def test_snapshot_marks_old_book_stale():
    scanner = LiveScanner()
    scanner.symbols = ["BTCUSDT"]
    scanner.books["BTCUSDT"] = {
        "symbol": "BTCUSDT",
        "mid": 100.0,
        "spread_pct": 0.1,
        "imbalance": 0.2,
        "shift": 0.0,
        "received_at_ms": int((time.time() - 30) * 1000),
        "exchange_event_at_ms": int((time.time() - 30) * 1000),
        "bid_levels": [(100.0, 1.0)],
        "ask_levels": [(101.0, 1.0)],
    }

    snapshot = run(scanner.snapshot())
    row = snapshot["markets"][0]

    assert row["stale"] is True
    assert snapshot["markets_with_live_book"] == 0


def test_orderbook_rejects_stale_book():
    scanner = LiveScanner()
    scanner.books["BTCUSDT"] = {
        "best_bid": 100.0,
        "best_ask": 101.0,
        "mid": 100.5,
        "spread_pct": 0.99,
        "imbalance": 0.2,
        "pressure": "BUY",
        "shift": 0.0,
        "received_at_ms": int((time.time() - 30) * 1000),
        "exchange_event_at_ms": int((time.time() - 30) * 1000),
        "bid_levels": [(100.0, 1.0)],
        "ask_levels": [(101.0, 1.0)],
    }

    result = run(scanner.orderbook("BTCUSDT"))

    assert result["available"] is False
    assert result["stale"] is True


def test_non_dict_websocket_message_is_ignored():
    scanner = LiveScanner()
    run(scanner.handle_message(json.dumps(["not", "an", "object"])))
    assert scanner.books == {}
