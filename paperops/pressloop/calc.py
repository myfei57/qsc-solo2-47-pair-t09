"""Allocation arithmetic for the press dryness loop.

The loop corrects a dryness error by moving several nips at once. Each nip takes
a weighted share of the correction, converted into a linear-load move through the
nip gain, then clamped to the per-cycle step and the nip's own load limits.
"""

from typing import Any, Mapping, Sequence


def error(target: float, measured: float) -> float:
    """Dryness points the sheet is short of target; positive means too wet."""
    return float(target) - float(measured)


def converged(err: float, tolerance: float) -> bool:
    """The loop holds once the error sits inside the tolerance band."""
    return abs(float(err)) <= float(tolerance)


def shares(weights: Sequence[float]) -> tuple[float, ...]:
    """Normalised allocation weights; equal shares when every weight is zero."""
    total = sum(float(weight) for weight in weights)
    if total <= 0.0:
        if not weights:
            return ()
        even = 1.0 / len(weights)
        return tuple(even for _ in weights)
    return tuple(float(weight) / total for weight in weights)


def clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def moves(
    err: float,
    nips: Sequence[Mapping[str, Any]],
    max_step: float,
) -> list[dict[str, Any]]:
    """Per-nip load moves that would deliver the dryness correction.

    Every move is reported as the nip sees it: the load before, the signed move
    and the load after, so the record shows exactly how hard each nip worked.
    """
    result: list[dict[str, Any]] = []
    weights = shares([float(nip["weight"]) for nip in nips])
    for nip, share in zip(nips, weights):
        gain = float(nip["gain"])
        load = float(nip["load"])
        want = float(err) * share / gain if gain > 0.0 else 0.0
        delta = clamp(want, -float(max_step), float(max_step))
        delta = clamp(load + delta, float(nip["min_load"]), float(nip["max_load"])) - load
        result.append(
            {
                "nip": str(nip["name"]),
                "before": load,
                "delta": delta,
                "after": load + delta,
            }
        )
    return result


def achieved(move_rows: Sequence[Mapping[str, Any]], nips: Sequence[Mapping[str, Any]]) -> float:
    """Dryness points the clamped moves actually deliver across the section."""
    gains = {str(nip["name"]): float(nip["gain"]) for nip in nips}
    return sum(float(move["delta"]) * gains.get(str(move["nip"]), 0.0) for move in move_rows)
