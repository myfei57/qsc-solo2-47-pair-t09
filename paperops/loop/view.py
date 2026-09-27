"""Read model for the press dryness loop."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LoopView:
    active: bool
    session: dict[str, Any] | None
    loads: dict[str, float]
    stable: dict[str, float]
    last_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "session": self.session,
            "loads": dict(self.loads),
            "stable": dict(self.stable),
            "last_status": self.last_status,
        }
