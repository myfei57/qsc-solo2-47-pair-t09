"""Closed-loop dryness control for the press section.

The press record only carries one total load, so when the machine speeds up the
dryness drifts and the operator trims by feel. This loop closes that gap: every
cycle it works out how much each nip should move toward the dryness target, and
when the target is not reached inside the allotted ticks it falls back to the
last steady nip loads. Every cycle lands in the record stream with its verdict
and each nip's move, so whether the loop converged and how hard every nip worked
can be looked up afterwards.
"""

from typing import Any, Mapping, Sequence

from ..clock import IdSequence, LogicalClock, parse_id
from ..errors import PreGateError
from ..event import EventBus, topics
from ..store import Journal, keys
from ..verdict import Band, VerdictHistory
from ..version import Confirmations, Generations
from . import calc
from .view import PressLoopView

CONFIG_NAME = "pressloop.config"
LOOP_NAME = "pressloop"
TARGET_BAND = Band(0.0, 100.0)

IDLE = "idle"
ADJUSTING = "adjusting"
CONVERGED = "converged"
FALLBACK = "fallback"

DEFAULT_NIPS: tuple[dict[str, Any], ...] = (
    {"name": "nip1", "load": 40.0, "min_load": 0.0, "max_load": 100.0, "weight": 0.2, "gain": 0.02},
    {"name": "nip2", "load": 50.0, "min_load": 0.0, "max_load": 100.0, "weight": 0.3, "gain": 0.02},
    {"name": "nip3", "load": 60.0, "min_load": 0.0, "max_load": 100.0, "weight": 0.5, "gain": 0.02},
)
DEFAULT_TOLERANCE = 0.3
DEFAULT_LIMIT_TICKS = 10
DEFAULT_MAX_STEP = 8.0


class PressLoopService:
    """Dryness target tracking, per-nip allocation and the timed fallback."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        generations: Generations,
        confirmations: Confirmations,
    ) -> None:
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._generations = generations
        self._confirmations = confirmations
        self._history = VerdictHistory("pressloop.convergence")
        self._ids = IdSequence("cyc")
        self._nips: list[dict[str, Any]] = [dict(nip) for nip in DEFAULT_NIPS]
        self._tolerance = DEFAULT_TOLERANCE
        self._limit_ticks = DEFAULT_LIMIT_TICKS
        self._max_step = DEFAULT_MAX_STEP
        self._status = IDLE
        self._target: float | None = None
        self._target_limit: int | None = None
        self._deadline: int | None = None
        self._episode = 0
        self._measured: float | None = None
        self._anchor: dict[str, float] | None = None
        self._last_cycle_id = ""

    # ----------------------------------------------------------------- reading

    def status(self) -> str:
        return self._status

    def target(self) -> float | None:
        return self._target

    def nips(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(nip) for nip in self._nips)

    def anchor(self) -> dict[str, float] | None:
        return dict(self._anchor) if self._anchor is not None else None

    def history(self) -> VerdictHistory:
        return self._history

    def cycles(self, limit: int = 0) -> list[dict[str, Any]]:
        """Recorded cycles, oldest first; the durable answer to what the loop did."""
        rows = [dict(entry.value) for entry in self._journal.history(keys.PRESSLOOP_CYCLE)]
        if limit > 0:
            rows = rows[-limit:]
        return rows

    def view(self) -> PressLoopView:
        return PressLoopView(
            status=self._status,
            target=self._target,
            tolerance=self._tolerance,
            limit_ticks=self._effective_limit(),
            max_step=self._max_step,
            episode=self._episode,
            deadline_tick=self._deadline,
            measured=self._measured,
            error=self._error(),
            nips=self.nips(),
            anchor=self.anchor(),
            cycles=len(self._journal.history(keys.PRESSLOOP_CYCLE)),
            last_cycle_id=self._last_cycle_id,
            generation=self._generations.current(CONFIG_NAME),
        )

    def preview(self, measured: float, target: float | None = None) -> dict[str, Any]:
        """The moves the loop would make for a reading, without applying them."""
        aim = self._target if target is None else float(target)
        if aim is None:
            raise PreGateError("pressloop: no dryness target is set")
        err = calc.error(aim, measured)
        settled = calc.converged(err, self._tolerance)
        move_rows = [] if settled else calc.moves(err, self._nips, self._max_step)
        return {
            "target": aim,
            "measured": float(measured),
            "error": err,
            "converged": settled,
            "requested": err,
            "achieved": calc.achieved(move_rows, self._nips),
            "moves": move_rows,
            "shares": list(calc.shares([nip["weight"] for nip in self._nips])),
            "status": self._status,
            "deadline_tick": self._deadline,
            "ticks_left": self._ticks_left(),
        }

    # ------------------------------------------------------------------ writes

    def configure(
        self,
        nips: Sequence[Mapping[str, Any]],
        tolerance: float,
        limit_ticks: int,
        max_step: float,
        confirmation_id: str,
    ) -> dict[str, Any]:
        """Replace the nip table and loop tuning under a confirmation.

        The loop drops back to idle: with new parameters the old target and
        anchor no longer describe a state the loop can stand behind.
        """
        self._confirmations.redeem(confirmation_id, CONFIG_NAME, LOOP_NAME)
        validated = self._validate_nips(nips)
        if tolerance <= 0:
            raise ValueError("tolerance must be positive")
        if limit_ticks < 1:
            raise ValueError("limit_ticks must be at least one tick")
        if max_step <= 0:
            raise ValueError("max_step must be positive")
        generation = self._generations.advance(CONFIG_NAME, "pressloop-config-generation")
        row = {
            "nips": validated,
            "tolerance": float(tolerance),
            "limit_ticks": int(limit_ticks),
            "max_step": float(max_step),
            "generation": generation,
            "tick": self._clock.tick,
        }
        with self._journal.transaction("pressloop-config") as batch:
            self._journal.append(keys.PRESSLOOP_CONFIG, row, batch, generation=generation)
        self._nips = [dict(nip) for nip in validated]
        self._tolerance = float(tolerance)
        self._limit_ticks = int(limit_ticks)
        self._max_step = float(max_step)
        self._status = IDLE
        self._target = None
        self._target_limit = None
        self._deadline = None
        self._episode = 0
        self._measured = None
        self._anchor = None
        self._write_status()
        self._bus.publish(
            topics.TOPIC_PRESSLOOP_CONFIG,
            {"generation": generation, "tick": self._clock.tick},
        )
        return dict(row)

    def set_target(self, target: float, limit_ticks: int | None = None) -> dict[str, Any]:
        """Aim the loop at a dryness target and open a fresh convergence window."""
        TARGET_BAND.require(target, "dryness target")
        if limit_ticks is not None and limit_ticks < 1:
            raise ValueError("limit_ticks must be at least one tick")
        self._target = float(target)
        self._target_limit = int(limit_ticks) if limit_ticks is not None else None
        if self._anchor is None:
            # the loads the machine was holding when the loop took over
            self._anchor = self._loads()
            self._write_anchor()
        self._enter_adjusting()
        row = {
            "target": self._target,
            "limit_ticks": self._effective_limit(),
            "limit_override": self._target_limit,
            "deadline_tick": self._deadline,
            "episode": self._episode,
            "tick": self._clock.tick,
        }
        with self._journal.transaction("pressloop-target") as batch:
            self._journal.append(keys.PRESSLOOP_TARGET, row, batch)
        self._bus.publish(topics.TOPIC_PRESSLOOP_TARGET, dict(row))
        return dict(row)

    def cycle(self, measured: float) -> dict[str, Any]:
        """Run one control cycle against a measured dryness and record the outcome."""
        if self._target is None:
            raise PreGateError("pressloop: no dryness target is set")
        if self._status == FALLBACK:
            raise PreGateError("pressloop: loop is in fallback; resume or set a new target")
        self._measured = float(measured)
        err = calc.error(self._target, self._measured)
        if calc.converged(err, self._tolerance):
            return self._converge(err)
        if self._status == CONVERGED:
            # drifted out of the band, as after a speed change: a fresh window
            self._enter_adjusting()
        if self._deadline is not None and self._clock.tick > self._deadline:
            return self._fall_back(err)
        move_rows = calc.moves(err, self._nips, self._max_step)
        self._apply(move_rows)
        return self._record_cycle(ADJUSTING, err, move_rows)

    def resume(self) -> dict[str, Any]:
        """Re-arm the loop after a fallback with a fresh convergence window."""
        if self._status != FALLBACK:
            raise PreGateError("pressloop: loop is not in fallback")
        self._enter_adjusting()
        return {
            "status": self._status,
            "episode": self._episode,
            "deadline_tick": self._deadline,
            "tick": self._clock.tick,
        }

    def reload(self) -> PressLoopView:
        """Restore the loop from the committed part of the stream."""
        config = self._journal.read(keys.PRESSLOOP_CONFIG)
        if config is not None:
            self._nips = [dict(nip) for nip in config["nips"]]
            self._tolerance = float(config["tolerance"])
            self._limit_ticks = int(config["limit_ticks"])
            self._max_step = float(config["max_step"])
        target = self._journal.read(keys.PRESSLOOP_TARGET)
        if target is not None:
            self._target = float(target["target"])
            override = target.get("limit_override")
            self._target_limit = int(override) if override is not None else None
        anchor = self._journal.read(keys.PRESSLOOP_ANCHOR)
        if anchor is not None:
            self._anchor = {str(name): float(load) for name, load in anchor["loads"].items()}
        status = self._journal.read(keys.PRESSLOOP_STATUS)
        if status is not None:
            self._status = str(status["status"])
            self._episode = int(status["episode"])
            deadline = status.get("deadline_tick")
            self._deadline = int(deadline) if deadline is not None else None
        cycles = self._journal.history(keys.PRESSLOOP_CYCLE)
        if cycles:
            last = cycles[-1].value
            self._last_cycle_id = str(last["cycle_id"])
            self._measured = float(last["measured"])
            loads = last.get("loads")
            if loads:
                by_name = {nip["name"]: nip for nip in self._nips}
                for name, load in loads.items():
                    if str(name) in by_name:
                        by_name[str(name)]["load"] = float(load)
            issued = parse_id(self._last_cycle_id)
            if issued is not None:
                self._ids.restore(issued)
        return self.view()

    # ---------------------------------------------------------------- internals

    def _converge(self, err: float) -> dict[str, Any]:
        transitioned = self._status != CONVERGED
        self._status = CONVERGED
        loads = self._loads()
        if loads != self._anchor:
            # the newest steady state becomes the fallback anchor
            self._anchor = loads
            self._write_anchor()
        if transitioned:
            self._write_status()
        return self._record_cycle(CONVERGED, err, [])

    def _fall_back(self, err: float) -> dict[str, Any]:
        """Give up on the window and put every nip back on the last steady load."""
        anchor = self._anchor or self._loads()
        move_rows = []
        for nip in self._nips:
            before = float(nip["load"])
            after = float(anchor[nip["name"]])
            move_rows.append({"nip": nip["name"], "before": before, "delta": after - before, "after": after})
            nip["load"] = after
        self._status = FALLBACK
        self._write_status()
        row = self._record_cycle(FALLBACK, err, move_rows, reason="deadline exceeded")
        self._bus.publish(
            topics.TOPIC_PRESSLOOP_FALLBACK,
            {"cycle_id": row["cycle_id"], "anchor": dict(anchor), "tick": self._clock.tick},
        )
        return row

    def _record_cycle(
        self,
        verdict: str,
        err: float,
        move_rows: list[dict[str, Any]],
        reason: str = "",
    ) -> dict[str, Any]:
        cycle_id = self._ids.next()
        row = {
            "cycle_id": cycle_id,
            "episode": self._episode,
            "tick": self._clock.tick,
            "verdict": verdict,
            "target": self._target,
            "measured": self._measured,
            "error": err,
            "requested": err,
            "achieved": calc.achieved(move_rows, self._nips),
            "moves": move_rows,
            "loads": self._loads(),
            "deadline_tick": self._deadline,
            "ticks_left": self._ticks_left(),
            "reason": reason,
        }
        with self._journal.transaction("pressloop-cycle") as batch:
            self._journal.append(keys.PRESSLOOP_CYCLE, row, batch)
        self._last_cycle_id = cycle_id
        self._history.record(verdict, self._journal.watermark, self._clock.tick, f"error={err:.2f}")
        self._bus.publish(topics.TOPIC_PRESSLOOP_CYCLE, dict(row))
        return row

    def _enter_adjusting(self) -> None:
        self._episode += 1
        self._status = ADJUSTING
        self._deadline = self._clock.tick + self._effective_limit()
        self._write_status()

    def _apply(self, move_rows: Sequence[Mapping[str, Any]]) -> None:
        by_name = {nip["name"]: nip for nip in self._nips}
        for move in move_rows:
            by_name[str(move["nip"])]["load"] = float(move["after"])

    def _loads(self) -> dict[str, float]:
        return {str(nip["name"]): float(nip["load"]) for nip in self._nips}

    def _error(self) -> float | None:
        if self._target is None or self._measured is None:
            return None
        return calc.error(self._target, self._measured)

    def _effective_limit(self) -> int:
        return self._target_limit if self._target_limit is not None else self._limit_ticks

    def _ticks_left(self) -> int | None:
        if self._deadline is None:
            return None
        return max(0, self._deadline - self._clock.tick)

    def _write_anchor(self) -> None:
        row = {"loads": dict(self._anchor), "measured": self._measured, "tick": self._clock.tick}
        with self._journal.transaction("pressloop-anchor") as batch:
            self._journal.append(keys.PRESSLOOP_ANCHOR, row, batch)

    def _write_status(self) -> None:
        row = {
            "status": self._status,
            "episode": self._episode,
            "deadline_tick": self._deadline,
            "tick": self._clock.tick,
        }
        with self._journal.transaction("pressloop-status") as batch:
            self._journal.append(keys.PRESSLOOP_STATUS, row, batch)

    @staticmethod
    def _validate_nips(nips: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not nips:
            raise ValueError("at least one nip is required")
        validated: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in nips:
            name = str(raw["name"]).strip()
            if not name:
                raise ValueError("nip name must not be empty")
            if name in seen:
                raise ValueError(f"duplicate nip {name}")
            seen.add(name)
            low = float(raw["min_load"])
            high = float(raw["max_load"])
            if low < 0:
                raise ValueError("nip min_load must not be negative")
            if low > high:
                raise ValueError(f"nip {name} min_load exceeds max_load")
            load = float(raw["load"])
            if not low <= load <= high:
                raise ValueError(f"nip {name} load {load} is outside [{low}, {high}]")
            gain = float(raw["gain"])
            if gain <= 0:
                raise ValueError(f"nip {name} gain must be positive")
            weight = float(raw["weight"])
            if weight < 0:
                raise ValueError(f"nip {name} weight must not be negative")
            validated.append(
                {
                    "name": name,
                    "load": load,
                    "min_load": low,
                    "max_load": high,
                    "weight": weight,
                    "gain": gain,
                }
            )
        return validated
