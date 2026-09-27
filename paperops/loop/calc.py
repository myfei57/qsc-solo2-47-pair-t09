"""Arithmetic for the press dryness closed loop."""

from typing import Sequence

EPSILON = 1e-9


def correction(error: float, gain: float) -> float:
    """Total linear-load change that removes a dryness error."""
    if gain <= 0:
        raise ValueError("press gain must be positive")
    return float(error) / float(gain)


def settled(errors: Sequence[float], tolerance: float, settle: int) -> bool:
    """True when the most recent `settle` errors all sit inside the tolerance."""
    if settle < 1:
        raise ValueError("settle must be at least one")
    if len(errors) < settle:
        return False
    return all(abs(error) <= tolerance for error in errors[-settle:])


def distribute(
    delta: float,
    loads: Sequence[float],
    lows: Sequence[float],
    highs: Sequence[float],
    weights: Sequence[float],
    max_step: float,
) -> list[float]:
    """Split a total load change across the nips.

    Every nip takes a share proportional to its weight, capped by its remaining
    headroom and by the per-step rate limit; whatever a capped nip cannot take
    is offered to the nips that still have room, until nothing can move.
    """
    count = len(loads)
    change = [0.0] * count
    used = [0.0] * count
    remaining = float(delta)
    for _ in range(count):
        if abs(remaining) <= EPSILON:
            break
        direction = 1.0 if remaining > 0.0 else -1.0
        room = []
        for index in range(count):
            if direction > 0:
                headroom = highs[index] - (loads[index] + change[index])
            else:
                headroom = (loads[index] + change[index]) - lows[index]
            capacity = min(headroom, max_step - used[index])
            if capacity > EPSILON:
                room.append((index, capacity))
        if not room:
            break
        total_weight = sum(max(weights[index], 0.0) for index, _ in room)
        moved = 0.0
        for index, capacity in room:
            if total_weight > 0.0:
                share = remaining * max(weights[index], 0.0) / total_weight
            else:
                share = remaining / len(room)
            give = direction * min(abs(share), capacity)
            change[index] += give
            used[index] += abs(give)
            moved += give
        remaining -= moved
        if abs(moved) <= EPSILON:
            break
    return change
