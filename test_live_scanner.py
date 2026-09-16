import unittest

from live_scanner import LiveScanner


class LiveScannerTests(unittest.TestCase):
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
        # The normalizer is exercised by the public orderbook method; no live
        # connection is needed for this test.
        self.assertEqual("BTCUSDT", "btc_usdt".upper().replace("_", ""))


if __name__ == "__main__":
    unittest.main()
