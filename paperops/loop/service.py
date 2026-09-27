"""Closed loop on press dryness with a timed fallback to the last stable state.

The press section used to keep one total pressure, so a dryness drift after a
speed-up was chased by hand. This service closes that loop: every control step
turns the dryness error into a total linear-load correction and splits it
across the nips. A session that cannot hold the target inside its time limit
falls back to the last loads that held steady. Engagements, steps and outcomes
are appended to the record stream, so whether a session converged and what each
nip contributed can be looked up afterwards.
"""

from typing import Any, Sequence

from ..clock import IdSequence, LogicalClock, parse_id
from ..errors import OutOfSequenceError
from ..event import EventBus, topics
from ..store import Journal, keys
from ..verdict import Band, VerdictHistory
from . import calc
from .view import LoopView

LOOP_NAME = "press.loop"
DRYNESS_BAND = Band(0.0, 100.0)
DEFAULT_NIPS: tuple[dict[str, Any], ...] = (
    {"name": "nip1", "low": 0.0, "high": 600.0, "weight": 1.0},
    {"name": "nip2", "low": 0.0, "high": 600.0, "weight": 1.0},
    {"name": "nip3", "low": 0.0, "high": 600.0, "weight": 1.0},
)
DEFAULT_WIDTH = 6.0
DEFAULT_TOLERANCE = 0.5
DEFAULT_TIME_LIMIT = 10
DEFAULT_SETTLE = 2
DEFAULT_MAX_STEP = 50.0

STATUS_ACTIVE = "active"
STATUS_CONVERGED = "converged"
STATUS_REVERTED = "reverted"
STATUS_NONE = "none"


class PressLoopService:
    """Dryness-target closed loop across the press nips."""

    def __init__(
        self,
        journal: Journal,
        bus: EventBus,
        clock: LogicalClock,
        ids: IdSequence,
        width: float = DEFAULT_WIDTH,
        nips: Sequence[dict[str, Any]] = DEFAULT_NIPS,
    ) -> None:
        if not nips:
            raise ValueError("the loop needs at least one nip")
        for nip in nips:
            if nip["low"] < 0 or nip["high"] < nip["low"]:
                raise ValueError("nip limits must be a non-negative low/high pair")
            if nip["weight"] < 0:
                raise ValueError("nip weight must not be negative")
        self._journal = journal
        self._bus = bus
        self._clock = clock
        self._ids = ids
        self._width = float(width)
        self._nips = tuple(dict(nip) for nip in nips)
        self._band = DRYNESS_BAND
        self._history = VerdictHistory(LOOP_NAME)
        self._loads = [0.0] * len(self._nips)
        self._stable = [0.0] * len(self._nips)
        self._session: dict[str, Any] | None = None
        self._last_status = STATUS_NONE
        self._seeded = False

    # ----------------------------------------------------------------- reading

    def nips(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(nip) for nip in self._nips)

    def loads(self) -> tuple[float, ...]:
        return tuple(self._loads)

    def stable_loads(self) -> tuple[float, ...]:
        return tuple(self._stable)

    def session(self) -> dict[str, Any] | None:
        return self._session_summary()

    def last_status(self) -> str:
        return self._last_status

    def history(self) -> VerdictHistory:
        return self._history

    def view(self) -> LoopView:
        return LoopView(
            active=self._session is not None,
            session=self._session_summary(),
            loads=self._named(self._loads),
            stable=self._named(self._stable),
            last_status=self._last_status,
        )

    def sessions(self) -> tuple[dict[str, Any], ...]:
        """Every session row ever committed, oldest first, with stream positions."""
        return tuple(
            {**entry.value, "seq": entry.seq}
            for entry in self._journal.history(keys.LOOP_SESSION)
        )

    def steps(self, session_id: str | None = None) -> tuple[dict[str, Any], ...]:
        """Every committed control step, optionally narrowed to one session."""
        rows = tuple(
            {**entry.value, "seq": entry.seq}
            for entry in self._journal.history(keys.LOOP_STEPS)
        )
        if session_id is None:
            return rows
        return tuple(row for row in rows if row["session_id"] == session_id)

    # ------------------------------------------------------------------ writes

    def engage(
        self,
        press: Any,
        target: float,
        tolerance: float = DEFAULT_TOLERANCE,
        time_limit: int = DEFAULT_TIME_LIMIT,
        settle: int = DEFAULT_SETTLE,
        max_step: float = DEFAULT_MAX_STEP,
    ) -> dict[str, Any]:
        """Open a control session against a dryness target.

        The time limit is counted in logical ticks: a session that has not
        converged when the clock passes its deadline falls back to the last
        stable loads on the next step.
        """
        if self._session is not None:
            raise OutOfSequenceError("press.loop: a session is already active")
        self._band.require(target, "dryness target")
        if tolerance <= 0:
            raise ValueError("tolerance must be positive")
        if time_limit < 1:
            raise ValueError("time limit must be at least one tick")
        if settle < 1:
            raise ValueError("settle must be at least one step")
        if max_step <= 0:
            raise ValueError("max step must be positive")
        self._sync_with_press(press)
        tick = self._clock.tick
        session = {
            "session_id": self._ids.next(),
            "status": STATUS_ACTIVE,
            "target": float(target),
            "tolerance": float(tolerance),
            "time_limit": int(time_limit),
            "settle": int(settle),
            "max_step": float(max_step),
            "start_tick": tick,
            "deadline": tick + int(time_limit),
            "end_tick": None,
            "initial_loads": self._named(self._loads),
            "final_loads": None,
            "added": None,
            "final_error": None,
            "reason": "",
            "errors": [],
        }
        self._session = session
        self._last_status = STATUS_ACTIVE
        row = self._session_row(session)
        with self._journal.transaction("loop-engage") as batch:
            self._journal.append(keys.LOOP_SESSION, row, batch)
            self._append_state(batch)
        self._history.record(STATUS_ACTIVE, self._journal.watermark, tick, f"target={row['target']}")
        self._bus.publish(topics.TOPIC_LOOP_ENGAGED, dict(row))
        return dict(row)

    def step(self, press: Any, measured: float) -> dict[str, Any]:
        """One control step: hold, correct, converge, or fall back when overtime."""
        session = self._require_session()
        self._band.require(measured, "measured dryness")
        measured = float(measured)
        error = session["target"] - measured
        session["errors"].append(error)
        if self._clock.tick > session["deadline"]:
            return self._revert(press, "time limit exceeded", measured)
        if calc.settled(session["errors"], session["tolerance"], session["settle"]):
            return self._converge(measured)
        correction = 0.0
        changes = [0.0] * len(self._nips)
        if abs(error) > session["tolerance"]:
            correction = calc.correction(error, press.gain())
            changes = calc.distribute(
                correction,
                self._loads,
                [nip["low"] for nip in self._nips],
                [nip["high"] for nip in self._nips],
                [nip["weight"] for nip in self._nips],
                session["max_step"],
            )
            self._loads = [load + change for load, change in zip(self._loads, changes)]
            self._apply(press)
        row = self._step_row(session, measured, error, correction, changes)
        with self._journal.transaction("loop-step") as batch:
            self._journal.append(keys.LOOP_STEPS, row, batch)
            self._append_state(batch)
        self._bus.publish(topics.TOPIC_LOOP_STEP, dict(row))
        return dict(row)

    def revert(self, press: Any, reason: str = "operator fallback") -> dict[str, Any]:
        """Fall back to the last stable loads before the time limit forces it."""
        self._require_session()
        return self._revert(press, reason)

    def reload(self, press: Any = None) -> LoopView:
        """Restore the nip loads, the stable fallback and any open session.

        A restored open session keeps its deadline; its settle window starts
        over, because the readings that built it are not replayed.
        """
        state = self._journal.read(keys.LOOP_STATE)
        if state is not None:
            self._loads = [float(value) for value in state["loads"]]
            self._stable = [float(value) for value in state["stable"]]
            self._seeded = True
            if press is not None:
                self._apply(press)
        rows = self.sessions()
        for row in rows:
            issued = parse_id(row["session_id"])
            if issued is not None:
                self._ids.restore(issued)
        if rows:
            latest = rows[-1]
            self._last_status = str(latest["status"])
            if latest["status"] == STATUS_ACTIVE:
                self._session = {
                    "session_id": latest["session_id"],
                    "status": STATUS_ACTIVE,
                    "target": float(latest["target"]),
                    "tolerance": float(latest["tolerance"]),
                    "time_limit": int(latest["time_limit"]),
                    "settle": int(latest["settle"]),
                    "max_step": float(latest["max_step"]),
                    "start_tick": int(latest["start_tick"]),
                    "deadline": int(latest["deadline"]),
                    "end_tick": None,
                    "initial_loads": dict(latest["initial_loads"]),
                    "final_loads": None,
                    "added": None,
                    "final_error": None,
                    "reason": "",
                    "errors": [],
                }
        return self.view()

    # -------------------------------------------------------------- arithmetic

    def correction_for(self, error: float, gain: float) -> float:
        return calc.correction(error, gain)

    def distribute_for(self, delta: float, max_step: float = DEFAULT_MAX_STEP) -> list[float]:
        return calc.distribute(
            delta,
            self._loads,
            [nip["low"] for nip in self._nips],
            [nip["high"] for nip in self._nips],
            [nip["weight"] for nip in self._nips],
            max_step,
        )

    def settled(self, errors: Sequence[float], tolerance: float, settle: int) -> bool:
        return calc.settled(errors, tolerance, settle)

    # ---------------------------------------------------------------- internals

    def _named(self, values: Sequence[float]) -> dict[str, float]:
        return {nip["name"]: float(value) for nip, value in zip(self._nips, values)}

    def _require_session(self) -> dict[str, Any]:
        if self._session is None:
            raise OutOfSequenceError("press.loop: no session is active")
        return self._session

    def _sync_with_press(self, press: Any) -> None:
        """Seed the nips from the press total when the operator moved it by hand."""
        total = press.nip_load(self._width)
        if self._seeded and abs(sum(self._loads) - total) <= 1e-6:
            return
        share = total / len(self._nips)
        self._loads = [min(max(share, nip["low"]), nip["high"]) for nip in self._nips]
        self._stable = list(self._loads)
        self._seeded = True

    def _apply(self, press: Any) -> None:
        """Push the nip total back onto the press so the section total stays coherent."""
        press.set_load(sum(self._loads) / self._width)

    def _append_state(self, batch: str) -> None:
        self._journal.append(
            keys.LOOP_STATE,
            {
                "loads": list(self._loads),
                "stable": list(self._stable),
                "tick": self._clock.tick,
            },
            batch,
        )

    def _session_summary(self) -> dict[str, Any] | None:
        session = self._session
        if session is None:
            return None
        return {
            "session_id": session["session_id"],
            "target": session["target"],
            "tolerance": session["tolerance"],
            "deadline": session["deadline"],
            "ticks_remaining": max(0, session["deadline"] - self._clock.tick),
            "steps": len(session["errors"]),
            "last_error": session["errors"][-1] if session["errors"] else None,
        }

    def _session_row(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": session["session_id"],
            "status": session["status"],
            "target": session["target"],
            "tolerance": session["tolerance"],
            "time_limit": session["time_limit"],
            "settle": session["settle"],
            "max_step": session["max_step"],
            "start_tick": session["start_tick"],
            "deadline": session["deadline"],
            "end_tick": session["end_tick"],
            "initial_loads": dict(session["initial_loads"]),
            "final_loads": session["final_loads"],
            "added": session["added"],
            "steps": len(session["errors"]),
            "final_error": session["final_error"],
            "reason": session["reason"],
            "tick": self._clock.tick,
        }

    def _step_row(
        self,
        session: dict[str, Any],
        measured: float | None,
        error: float | None,
        correction: float,
        changes: Sequence[float],
    ) -> dict[str, Any]:
        return {
            "session_id": session["session_id"],
            "status": session["status"],
            "measured": measured,
            "error": error,
            "correction": correction,
            "changes": self._named(changes),
            "loads": self._named(self._loads),
            "unallocated": correction - sum(changes),
            "tick": self._clock.tick,
        }

    def _converge(self, measured: float) -> dict[str, Any]:
        session = self._require_session()
        session["status"] = STATUS_CONVERGED
        session["end_tick"] = self._clock.tick
        session["final_error"] = session["errors"][-1]
        self._stable = list(self._loads)
        step_row = self._step_row(
            session, measured, session["final_error"], 0.0, [0.0] * len(self._nips)
        )
        row = self._finalize(session, step_row)
        self._bus.publish(topics.TOPIC_LOOP_STEP, dict(step_row))
        self._bus.publish(topics.TOPIC_LOOP_CONVERGED, dict(row))
        return dict(row)

    def _revert(self, press: Any, reason: str, measured: float | None = None) -> dict[str, Any]:
        session = self._require_session()
        session["status"] = STATUS_REVERTED
        session["end_tick"] = self._clock.tick
        session["reason"] = reason
        if session["errors"]:
            session["final_error"] = session["errors"][-1]
        changes = [stable - load for stable, load in zip(self._stable, self._loads)]
        self._loads = list(self._stable)
        self._apply(press)
        step_row = self._step_row(
            session,
            measured,
            session["final_error"],
            sum(changes),
            changes,
        )
        row = self._finalize(session, step_row)
        self._bus.publish(topics.TOPIC_LOOP_STEP, dict(step_row))
        self._bus.publish(topics.TOPIC_LOOP_REVERTED, dict(row))
        return dict(row)

    def _finalize(self, session: dict[str, Any], step_row: dict[str, Any]) -> dict[str, Any]:
        final_loads = self._named(self._loads)
        session["final_loads"] = final_loads
        session["added"] = {
            name: final_loads[name] - session["initial_loads"][name] for name in final_loads
        }
        row = self._session_row(session)
        with self._journal.transaction("loop-close") as batch:
            self._journal.append(keys.LOOP_STEPS, step_row, batch)
            self._journal.append(keys.LOOP_SESSION, row, batch)
            self._append_state(batch)
        detail = f"target={session['target']}"
        if session["final_error"] is not None:
            detail += f" error={session['final_error']:.2f}"
        self._history.record(
            session["status"], self._journal.watermark, self._clock.tick, detail
        )
        self._session = None
        self._last_status = session["status"]
        return row
