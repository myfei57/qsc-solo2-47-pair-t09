"""Read model for the press dryness loop."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PressLoopView:
    status: str
    target: float | None
    tolerance: float
    limit_ticks: int
    max_step: float
    episode: int
    deadline_tick: int | None
    measured: float | None
    error: float | None
    nips: tuple[dict[str, Any], ...]
    anchor: dict[str, float] | None
    cycles: int
    last_cycle_id: str
    generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "target": self.target,
            "tolerance": self.tolerance,
            "limit_ticks": self.limit_ticks,
            "max_step": self.max_step,
            "episode": self.episode,
            "deadline_tick": self.deadline_tick,
            "measured": self.measured,
            "error": self.error,
            "nips": [dict(nip) for nip in self.nips],
            "anchor": dict(self.anchor) if self.anchor is not None else None,
            "cycles": self.cycles,
            "last_cycle_id": self.last_cycle_id,
            "generation": self.generation,
        }
