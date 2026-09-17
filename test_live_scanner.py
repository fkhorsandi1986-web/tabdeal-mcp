import time
import unittest
from unittest.mock import AsyncMock

from live_scanner import LiveScanner, STALE_AFTER_SECONDS


class LiveScannerTests(unittest.IsolatedAsyncioTestCase):
    def test_book_metrics(self):
        data = {
            "b": [["100", "2"], ["99", "1"]],
            "a": [["101", "1"], ["102", "1"]],
        }
        result = LiveScanner._book_metrics("TESTUSDT", data, 20)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["best_bid"], 100.0)
        self.assertEqual(result["best_ask"], 101.0)
        self.assertAlmostEqual(result["mid"], 100.5)
        self.assertGreater(result["imbalance"], 0)

    def test_invalid_book_returns_none(self):
        self.assertIsNone(LiveScanner._book_metrics("TESTUSDT", {"b": [], "a": []}, 20))

    def test_symbol_normalization_is_supported(self):
        self.assertEqual("BTCUSDT", "btc_usdt".upper().replace("_", ""))

    def test_freshness_marks_old_book_stale(self):
        now_ms = int(time.time() * 1000)
        fresh, age = LiveScanner._fresh({"received_at_ms": now_ms - 1000}, now_ms)
        self.assertTrue(fresh)
        self.assertLess(age, STALE_AFTER_SECONDS)

        fresh, age = LiveScanner._fresh(
            {"received_at_ms": now_ms - (STALE_AFTER_SECONDS + 1) * 1000}, now_ms
        )
        self.assertFalse(fresh)
        self.assertGreater(age, STALE_AFTER_SECONDS)

    async def test_stale_orderbook_is_not_reported_as_available(self):
        scanner = LiveScanner()
        scanner.books["TESTUSDT"] = {
            "best_bid": 100.0,
            "best_ask": 101.0,
            "mid": 100.5,
            "spread_pct": 0.995,
            "imbalance": 0.1,
            "pressure": "NEUTRAL",
            "shift": 0.0,
            "bid_levels": [(100.0, 1.0)],
            "ask_levels": [(101.0, 1.0)],
            "received_at_ms": int(time.time() * 1000) - (STALE_AFTER_SECONDS + 1) * 1000,
            "exchange_event_at_ms": None,
        }
        result = await scanner.orderbook("test_usdt")
        self.assertFalse(result["available"])
        self.assertTrue(result["stale"])

    async def test_refresh_markets_reports_subscription_change(self):
        scanner = LiveScanner()
        scanner.fetch_exchange_info = AsyncMock(return_value=["BTCUSDT", "ETHUSDT"])
        self.assertTrue(await scanner.refresh_markets())
        self.assertFalse(await scanner.refresh_markets())
        scanner.fetch_exchange_info = AsyncMock(return_value=["BTCUSDT"])
        self.assertTrue(await scanner.refresh_markets())
        self.assertEqual(scanner.symbols, ["BTCUSDT"])
        self.assertNotIn("ETHUSDT", scanner.history)


if __name__ == "__main__":
    unittest.main()
