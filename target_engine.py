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
    """Build a transparent target using book direction, depth quality and persistence."""
    if item.get("stale"):
        return None
    try:
        mid = float(item["mid"])
        best_bid = float(item.get("best_bid", mid))
        best_ask = float(item.get("best_ask", mid))
        spread_pct = float(item.get("spread_pct", 0.0))
        imbalance = float(item.get("imbalance", 0.0))
        shift = float(item.get("shift", 0.0))
        age = float(item.get("live_age_seconds", 999999))
        persistence = float(item.get("persistence", 1.0))
        opposite_wall_share = float(item.get("opposite_wall_share", 0.0))
        price_change_pct = float(item.get("price_change_pct", 0.0))
        largest_bid_share = float(item.get("largest_bid_share", 0.0))
        largest_ask_share = float(item.get("largest_ask_share", 0.0))
        total_depth_value = float(item.get("total_depth_value", 0.0))
    except (KeyError, TypeError, ValueError):
        return None

    if mid <= 0 or age > MAX_LIVE_AGE_SECONDS or spread_pct <= 0 or spread_pct > MAX_TARGET_SPREAD_PCT:
        return None
    if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
        return None
    if total_depth_value < 0:
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
    if persistence < MIN_PERSISTENCE:
        return None
    if opposite_wall_share > MAX_OPPOSITE_WALL_SHARE:
        return None

    entry_side_price = best_ask if direction == "LONG" else best_bid

    # A very concentrated wall can be useful liquidity but is also easier to spoof.
    dominant_wall_share = largest_bid_share if direction == "LONG" else largest_ask_share
    wall_risk = _clamp((dominant_wall_share - 0.25) / 0.50)
    persistence_quality = _clamp((persistence - MIN_PERSISTENCE) / 0.70)
    spread_quality = _clamp(1.0 - spread_pct / MAX_TARGET_SPREAD_PCT)
    shift_quality = _clamp(abs(shift) / 0.10)
    book_quality = _clamp((abs_imbalance - MIN_IMBALANCE) / (1.0 - MIN_IMBALANCE))

    # Price movement is a confirmation signal, not a reason by itself to chase.
    price_confirmation = _clamp(abs(price_change_pct) / 1.0)
    if (direction == "LONG" and price_change_pct > 0) or (direction == "SHORT" and price_change_pct < 0):
        price_confirmation *= 1.0
    else:
        price_confirmation *= 0.45

    strength = (
        book_quality * 35.0
        + persistence_quality * 25.0
        + shift_quality * 15.0
        + spread_quality * 10.0
        + price_confirmation * 10.0
        + (1.0 - wall_risk) * 5.0
    )

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

    risk_distance_pct = invalidation_pct
    reward_distance_pct = target_pcts[0]
    risk_reward = reward_distance_pct / risk_distance_pct if risk_distance_pct else 0.0

    quality_flags = []
    if abs_imbalance >= STRONG_IMBALANCE:
        quality_flags.append("strong_book_imbalance")
    if persistence >= 0.70:
        quality_flags.append("persistent_direction")
    elif persistence >= MIN_PERSISTENCE:
        quality_flags.append("developing_persistence")
    if abs(shift) >= MIN_SHIFT_CONFIRMATION:
        quality_flags.append("measurable_shift")
    if spread_pct <= 0.70:
        quality_flags.append("tight_spread")
    elif spread_pct > 1.20:
        quality_flags.append("wide_spread_watch")
    if wall_risk >= 0.60:
        quality_flags.append("wall_concentration_risk")
    if price_confirmation >= 0.60:
        quality_flags.append("price_confirms_direction")
    elif abs(price_change_pct) >= 0.50:
        quality_flags.append("price_movement_needs_caution")
    if not quality_flags:
        quality_flags.append("basic_directional_book")

    reasons.extend([
        f"direction persistence {persistence:.0%}",
        f"opposite-wall share {opposite_wall_share:.0%}",
        f"depth-wall concentration risk {wall_risk:.0%}",
    ])
    if price_confirmation >= 0.60:
        reasons.append("recent price movement agrees with direction")
    elif abs(price_change_pct) >= 0.50:
        reasons.append("recent price movement is strong enough to avoid chasing blindly")

    return {
        "symbol": item.get("symbol"),
        "direction": direction,
        "entry_reference": round(mid, 12),
        "entry_side_price": round(entry_side_price, 12),
        "target_1": targets[0],
        "target_2": targets[1],
        "target_3": targets[2],
        "invalidation": invalidation,
        "target_move_pct": [round(p, 4) for p in target_pcts],
        "invalidation_move_pct": round(invalidation_pct, 4),
        "risk_reward_to_target_1": round(risk_reward, 3),
        "signal_strength": round(max(0.0, min(100.0, strength)), 2),
        "live_age_seconds": round(age, 3),
        "spread_pct": round(spread_pct, 6),
        "imbalance": round(imbalance, 6),
        "shift": round(shift, 6),
        "persistence": round(persistence, 4),
        "price_change_pct": round(price_change_pct, 6),
        "largest_bid_share": round(largest_bid_share, 6),
        "largest_ask_share": round(largest_ask_share, 6),
        "opposite_wall_share": round(opposite_wall_share, 6),
        "wall_risk": round(wall_risk, 4),
        "total_depth_value": round(total_depth_value, 8),
        "quality_flags": quality_flags,
        "reasons": reasons,
        "method": "live order-book imbalance + depth quality + persistence + price confirmation + spread + wall-risk; public trade-flow confirmation when available",
        "not_a_prediction": True,
    }


def build_targets(markets: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    targets = [target for item in markets if (target := build_target(item)) is not None]
    # Combined quality first; then tighter spreads and fresher data.
    targets.sort(
        key=lambda x: (
            -x["signal_strength"],
            x["spread_pct"],
            -x["persistence"],
            x["live_age_seconds"],
        )
    )
    return targets[: max(1, min(int(limit), 100))]
