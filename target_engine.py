from __future__ import annotations

from typing import Any

MAX_TARGET_SPREAD_PCT = 2.0
MIN_IMBALANCE = 0.20
STRONG_IMBALANCE = 0.45
MIN_SHIFT_CONFIRMATION = 0.005


def build_target(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build a transparent analytical target from a live order book."""
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

    if mid <= 0 or age > 20 or spread_pct <= 0 or spread_pct > MAX_TARGET_SPREAD_PCT:
        return None

    abs_imbalance = abs(imbalance)
    if abs_imbalance < MIN_IMBALANCE:
        return None

    if imbalance >= MIN_IMBALANCE:
        direction = "LONG"
        confirmed = shift >= MIN_SHIFT_CONFIRMATION or abs_imbalance >= STRONG_IMBALANCE
        reasons = ["positive order-book imbalance"]
        if shift > 0:
            reasons.append("buy-side imbalance is strengthening")
    elif imbalance <= -MIN_IMBALANCE:
        direction = "SHORT"
        confirmed = shift <= -MIN_SHIFT_CONFIRMATION or abs_imbalance >= STRONG_IMBALANCE
        reasons = ["negative order-book imbalance"]
        if shift < 0:
            reasons.append("sell-side imbalance is strengthening")
    else:
        return None

    if not confirmed:
        return None

    spread_penalty = min(0.25, spread_pct / MAX_TARGET_SPREAD_PCT * 0.25)
    base_pct = max(0.30, min(1.50, 0.35 + abs_imbalance * 0.80 + abs(shift) * 0.40))
    base_pct *= 1.0 - spread_penalty
    target_pcts = [base_pct, base_pct * 1.75, base_pct * 2.50]

    invalidation_pct = max(0.35, min(0.60, abs_imbalance * 0.50 * 100))
    if direction == "LONG":
        targets = [round(mid * (1 + p / 100), 12) for p in target_pcts]
        invalidation = round(mid * (1 - invalidation_pct / 100), 12)
    else:
        targets = [round(mid * (1 - p / 100), 12) for p in target_pcts]
        invalidation = round(mid * (1 + invalidation_pct / 100), 12)

    # 70% imbalance, 20% shift persistence, 10% spread quality.
    imbalance_component = min(1.0, abs_imbalance)
    shift_component = min(1.0, abs(shift) / 0.10)
    spread_component = max(0.0, 1.0 - spread_pct / MAX_TARGET_SPREAD_PCT)
    strength = (
        imbalance_component * 70.0
        + shift_component * 20.0
        + spread_component * 10.0
    )

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
        "signal_strength": round(max(0.0, min(100.0, strength)), 2),
        "live_age_seconds": round(age, 3),
        "spread_pct": round(spread_pct, 6),
        "imbalance": round(imbalance, 6),
        "shift": round(shift, 6),
        "reasons": reasons,
        "method": "live order-book imbalance + shift + spread quality; trade-flow confirmation when available",
        "not_a_prediction": True,
    }


def build_targets(markets: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    targets = [target for item in markets if (target := build_target(item)) is not None]
    targets.sort(key=lambda x: x["signal_strength"], reverse=True)
    return targets[: max(1, min(int(limit), 100))]
