from __future__ import annotations

from typing import Any


def build_target(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build a transparent, rule-based analytical target from a live order book."""
    if item.get("stale"):
        return None
    try:
        mid = float(item["mid"])
        spread_pct = float(item.get("spread_pct", 0.0))
        imbalance = float(item.get("imbalance", 0.0))
        shift = float(item.get("shift", 0.0))
        age = float(item.get("live_age_seconds", 999999))
    except (KeyError, TypeError, ValueError):
        return None
    if mid <= 0 or age > 20:
        return None

    if imbalance >= 0.15 and shift >= -0.05:
        direction = "LONG"
        side_score = imbalance + max(shift, 0.0)
        invalidation_pct = max(0.20, min(0.60, abs(imbalance) * 0.50 * 100))
        base_pct = max(0.25, min(1.50, 0.35 + abs(imbalance) * 0.80 + max(shift, 0.0) * 0.40))
        reasons = ["positive order-book imbalance"]
        if shift > 0:
            reasons.append("buy-side imbalance is strengthening")
    elif imbalance <= -0.15 and shift <= 0.05:
        direction = "SHORT"
        side_score = abs(imbalance) + max(-shift, 0.0)
        invalidation_pct = max(0.20, min(0.60, abs(imbalance) * 0.50 * 100))
        base_pct = max(0.25, min(1.50, 0.35 + abs(imbalance) * 0.80 + max(-shift, 0.0) * 0.40))
        reasons = ["negative order-book imbalance"]
        if shift < 0:
            reasons.append("sell-side imbalance is strengthening")
    else:
        return None

    base_pct = max(base_pct, min(0.50, spread_pct * 2.0))
    target_pcts = [base_pct, base_pct * 1.75, base_pct * 2.50]
    if direction == "LONG":
        targets = [round(mid * (1 + p / 100), 12) for p in target_pcts]
        invalidation = round(mid * (1 - invalidation_pct / 100), 12)
    else:
        targets = [round(mid * (1 - p / 100), 12) for p in target_pcts]
        invalidation = round(mid * (1 + invalidation_pct / 100), 12)

    strength = max(0.0, min(100.0, side_score * 100.0))
    return {
        "symbol": item.get("symbol"),
        "direction": direction,
        "entry_reference": round(mid, 12),
        "target_1": targets[0],
        "target_2": targets[1],
        "target_3": targets[2],
        "invalidation": invalidation,
        "target_move_pct": [round(p, 4) for p in target_pcts],
        "invalidation_move_pct": round(invalidation_pct, 4),
        "signal_strength": round(strength, 2),
        "live_age_seconds": round(age, 3),
        "spread_pct": spread_pct,
        "imbalance": imbalance,
        "shift": shift,
        "reasons": reasons,
        "method": "live order-book imbalance + imbalance shift; analytical targets only",
        "not_a_prediction": True,
    }


def build_targets(markets: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    targets = [target for item in markets if (target := build_target(item)) is not None]
    targets.sort(key=lambda x: x["signal_strength"], reverse=True)
    return targets[: max(1, min(int(limit), 100))]
