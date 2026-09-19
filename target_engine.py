from __future__ import annotations
from typing import Any

MAX_TARGET_SPREAD_PCT = 2.0
MIN_IMBALANCE = 0.20
STRONG_IMBALANCE = 0.45
MIN_SHIFT_CONFIRMATION = 0.005
MAX_LIVE_AGE_SECONDS = 20.0
MIN_PERSISTENCE = 0.30
MAX_OPPOSITE_WALL_SHARE = 0.55


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def build_target(item: dict[str, Any]) -> dict[str, Any] | None:
    """Build a transparent, anti-chase target from live microstructure."""
    if item.get("stale"):
        return None
    try:
        mid = float(item["mid"]); best_bid = float(item.get("best_bid", mid)); best_ask = float(item.get("best_ask", mid))
        spread = float(item.get("spread_pct", 0)); imbalance = float(item.get("imbalance", 0))
        near_imbalance = float(item.get("near_imbalance", imbalance)); shift = float(item.get("shift", 0))
        age = float(item.get("live_age_seconds", 999999)); persistence = float(item.get("persistence", 0))
        persistence30 = float(item.get("persistence_30", persistence)); opposite = float(item.get("opposite_wall_share", 0))
        momentum5 = float(item.get("momentum_5_pct", item.get("price_change_pct", 0)))
        momentum15 = float(item.get("momentum_15_pct", momentum5))
        imbalance_vol = float(item.get("imbalance_volatility", 0))
        bid_share = float(item.get("largest_bid_share", 0)); ask_share = float(item.get("largest_ask_share", 0))
        depth = float(item.get("total_depth_value", 0))
    except (KeyError, TypeError, ValueError):
        return None

    if not (mid > 0 and best_bid > 0 and best_ask >= best_bid and 0 < spread <= MAX_TARGET_SPREAD_PCT):
        return None
    if age > MAX_LIVE_AGE_SECONDS or abs(imbalance) < MIN_IMBALANCE or depth <= 0:
        return None

    direction = "LONG" if imbalance >= MIN_IMBALANCE else "SHORT"
    sign = 1 if direction == "LONG" else -1
    if sign * shift < MIN_SHIFT_CONFIRMATION and abs(imbalance) < STRONG_IMBALANCE:
        return None
    if persistence < MIN_PERSISTENCE or opposite > MAX_OPPOSITE_WALL_SHARE:
        return None

    dominant_share = bid_share if direction == "LONG" else ask_share
    wall_risk = _clamp((dominant_share - 0.25) / 0.50)
    spread_quality = _clamp(1 - spread / 2.0)
    book_quality = _clamp((abs(imbalance) - MIN_IMBALANCE) / (1 - MIN_IMBALANCE))
    near_quality = _clamp((sign * near_imbalance - 0.10) / 0.70)
    persistence_quality = _clamp((persistence - MIN_PERSISTENCE) / 0.70)
    multiwindow_quality = _clamp((persistence30 - 0.25) / 0.75)
    shift_quality = _clamp(abs(shift) / 0.10)

    aligned_momentum = sign * momentum5
    momentum_confirmation = _clamp(aligned_momentum / 1.0)
    divergence = sign * (momentum5 - momentum15)
    anti_chase = _clamp(max(0.0, aligned_momentum - 1.0) / 2.0)

    strength = (
        book_quality * 25 + near_quality * 15 + persistence_quality * 15 +
        multiwindow_quality * 10 + shift_quality * 10 + spread_quality * 10 +
        momentum_confirmation * 10 + (1 - wall_risk) * 5
    )

    # If price already ran too far in the short window, downgrade instead of chasing.
    chase_risk = _clamp((aligned_momentum - 1.0) / 2.0)
    if chase_risk > 0.65:
        strength *= 0.80

    base_pct = max(0.30, min(1.50, 0.35 + abs(imbalance) * 0.80 + abs(shift) * 0.40))
    base_pct *= 1 - min(0.25, spread / 2.0 * 0.25)
    targets_pct = [base_pct, base_pct * 1.75, base_pct * 2.50]
    invalidation_pct = max(0.35, min(0.60, abs(imbalance) * 0.50 * 100))

    if direction == "LONG":
        targets = [round(mid * (1 + p / 100), 12) for p in targets_pct]
        invalidation = round(mid * (1 - invalidation_pct / 100), 12)
        pullback_entry = round(max(best_bid, mid * (1 - min(0.35, base_pct * 0.35) / 100)), 12)
    else:
        targets = [round(mid * (1 - p / 100), 12) for p in targets_pct]
        invalidation = round(mid * (1 + invalidation_pct / 100), 12)
        pullback_entry = round(min(best_ask, mid * (1 + min(0.35, base_pct * 0.35) / 100)), 12)

    rr = targets_pct[0] / invalidation_pct if invalidation_pct else 0

    flags = []
    if abs(imbalance) >= STRONG_IMBALANCE: flags.append("strong_book_imbalance")
    if abs(near_imbalance) >= 0.30: flags.append("near_touch_confirmation")
    if persistence >= 0.70: flags.append("persistent_direction")
    if persistence30 >= 0.60: flags.append("multi_window_confirmation")
    if abs(shift) >= MIN_SHIFT_CONFIRMATION: flags.append("measurable_shift")
    if spread <= 0.70: flags.append("tight_spread")
    if wall_risk >= 0.60: flags.append("wall_concentration_risk")
    if chase_risk >= 0.50: flags.append("chase_risk")
    if divergence < -0.50: flags.append("momentum_divergence")

    reasons = [
        f"{'buy' if direction == 'LONG' else 'sell'}-side depth imbalance {imbalance:+.2f}",
        f"near-touch imbalance {near_imbalance:+.2f}",
        f"persistence 10/30 {persistence:.0%}/{persistence30:.0%}",
        f"spread {spread:.3f}%",
    ]
    if chase_risk >= 0.50: reasons.append("price has already moved fast; prefer pullback over chasing")
    if divergence < -0.50: reasons.append("short-window momentum is diverging from the broader window")
    if wall_risk >= 0.60: reasons.append("dominant wall concentration raises spoofing/reversal risk")

    return {
        "symbol": item.get("symbol"), "direction": direction,
        "entry_reference": round(mid, 12), "entry_side_price": round(best_ask if direction == "LONG" else best_bid, 12),
        "pullback_entry_reference": pullback_entry,
        "target_1": targets[0], "target_2": targets[1], "target_3": targets[2],
        "invalidation": invalidation, "target_move_pct": [round(x, 4) for x in targets_pct],
        "invalidation_move_pct": round(invalidation_pct, 4), "risk_reward_to_target_1": round(rr, 3),
        "signal_strength": round(max(0, min(100, strength)), 2),
        "chase_risk": round(chase_risk, 4), "momentum_5_pct": round(momentum5, 6),
        "momentum_15_pct": round(momentum15, 6), "momentum_divergence": round(divergence, 6),
        "spread_pct": round(spread, 6), "imbalance": round(imbalance, 6),
        "near_imbalance": round(near_imbalance, 6), "shift": round(shift, 6),
        "persistence": round(persistence, 4), "persistence_30": round(persistence30, 4),
        "largest_bid_share": round(bid_share, 6), "largest_ask_share": round(ask_share, 6),
        "opposite_wall_share": round(opposite, 6), "wall_risk": round(wall_risk, 4),
        "total_depth_value": round(depth, 8), "quality_flags": flags or ["basic_directional_book"],
        "reasons": reasons,
        "method": "multi-window order-book + near-touch depth + persistence + momentum + spread + wall/chase risk + public trade-flow confirmation",
        "not_a_prediction": True,
    }


def build_targets(markets: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    targets = [t for item in markets if (t := build_target(item)) is not None]
    targets.sort(key=lambda x: (-x["signal_strength"], x["chase_risk"], x["spread_pct"], -x["persistence"], x["live_age_seconds"]))
    return targets[:max(1, min(int(limit), 100))]
