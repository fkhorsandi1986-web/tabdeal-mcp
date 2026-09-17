from target_engine import build_target, build_targets


def base_item(**overrides):
    item = {
        "symbol": "BTCUSDT",
        "mid": 100.0,
        "spread_pct": 0.10,
        "imbalance": 0.25,
        "shift": 0.02,
        "live_age_seconds": 2.0,
        "stale": False,
    }
    item.update(overrides)
    return item


def test_build_target_creates_long_with_upside_targets():
    result = build_target(base_item())

    assert result is not None
    assert result["direction"] == "LONG"
    assert result["entry_reference"] == 100.0
    assert result["target_1"] > result["entry_reference"]
    assert result["target_2"] > result["target_1"]
    assert result["target_3"] > result["target_2"]
    assert result["invalidation"] < result["entry_reference"]
    assert result["not_a_prediction"] is True


def test_build_target_creates_short_with_downside_targets():
    result = build_target(
        base_item(imbalance=-0.30, shift=-0.03)
    )

    assert result is not None
    assert result["direction"] == "SHORT"
    assert result["target_1"] < result["entry_reference"]
    assert result["target_2"] < result["target_1"]
    assert result["target_3"] < result["target_2"]
    assert result["invalidation"] > result["entry_reference"]


def test_build_target_rejects_stale_data():
    assert build_target(base_item(stale=True)) is None


def test_build_target_rejects_old_data():
    assert build_target(base_item(live_age_seconds=21)) is None


def test_build_target_rejects_neutral_book():
    assert build_target(base_item(imbalance=0.05, shift=0.01)) is None


def test_build_targets_sorts_by_signal_strength_and_limits_results():
    markets = [
        base_item(symbol="A", imbalance=0.16, shift=0.00),
        base_item(symbol="B", imbalance=0.40, shift=0.10),
        base_item(symbol="C", imbalance=-0.50, shift=-0.10),
        base_item(symbol="D", imbalance=0.01, shift=0.00),
    ]

    results = build_targets(markets, limit=2)

    assert len(results) == 2
    assert results[0]["symbol"] == "B"
    assert results[1]["symbol"] == "C"
