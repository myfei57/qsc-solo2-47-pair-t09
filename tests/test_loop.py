"""Closed loop on press dryness: corrections, timed fallback and the record."""

import pytest

from paperops.loop import calc

from support import advance_clock, build, ok, refused


def engage(console, target=46.0, **options):
    body = {"target": target}
    body.update(options)
    return ok(console, "POST", "/api/loop/engage", body)


def test_engage_seeds_the_nips_from_the_press_total(console, services):
    ok(console, "POST", "/api/press/load", {"load": 45})
    row = engage(console)
    assert row["status"] == "active"
    assert row["initial_loads"] == {"nip1": 90.0, "nip2": 90.0, "nip3": 90.0}
    assert row["deadline"] == row["start_tick"] + 10
    view = ok(console, "GET", "/api/loop/state")
    assert view["active"] is True
    assert view["loads"] == row["initial_loads"]
    assert view["stable"] == row["initial_loads"]
    assert services.press.load() == 45.0


def test_step_turns_the_error_into_nip_corrections(console, services):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console)
    row = ok(console, "POST", "/api/loop/step", {"measured": 45.0})
    # an error of 1.0 with gain 0.02 asks for 50 kN/m spread across three nips
    assert row["error"] == 1.0
    assert row["correction"] == 50.0
    assert round(row["changes"]["nip1"], 2) == 16.67
    assert row["unallocated"] == 0.0
    assert round(row["loads"]["nip1"], 2) == 106.67
    assert round(services.press.load(), 2) == 53.33


def test_readings_inside_the_deadband_are_held_not_corrected(console, services):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console)
    row = ok(console, "POST", "/api/loop/step", {"measured": 45.7})
    assert row["error"] == pytest.approx(0.3)
    assert row["correction"] == 0.0
    assert row["loads"] == {"nip1": 90.0, "nip2": 90.0, "nip3": 90.0}
    assert services.press.load() == 45.0


def test_session_converges_after_a_settled_run(console, services):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console, settle=2)
    ok(console, "POST", "/api/loop/step", {"measured": 45.7})
    row = ok(console, "POST", "/api/loop/step", {"measured": 45.8})
    assert row["status"] == "converged"
    assert row["steps"] == 2
    assert row["final_error"] == pytest.approx(0.2)
    view = ok(console, "GET", "/api/loop/state")
    assert view["active"] is False
    assert view["last_status"] == "converged"
    assert view["stable"] == view["loads"]
    history = ok(console, "GET", "/api/verdict/history?subject=press.loop")
    assert history["current"] == "converged"


def test_corrections_accumulate_until_the_target_holds(console, services):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console)
    ok(console, "POST", "/api/loop/step", {"measured": 45.0})
    ok(console, "POST", "/api/loop/step", {"measured": 45.9})
    row = ok(console, "POST", "/api/loop/step", {"measured": 46.0})
    assert row["status"] == "converged"
    assert round(row["added"]["nip1"], 2) == 16.67
    assert round(row["final_loads"]["nip1"], 2) == 106.67
    # the converged loads become the stable fallback for the next session
    assert ok(console, "GET", "/api/loop/state")["stable"]["nip1"] == pytest.approx(
        row["final_loads"]["nip1"]
    )


def test_session_falls_back_to_the_stable_state_when_time_runs_out(console, services):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console, target=48.0, time_limit=2)
    first = ok(console, "POST", "/api/loop/step", {"measured": 45.0})
    assert first["loads"]["nip1"] == 140.0
    assert services.press.load() == 70.0
    advance_clock(console, 3)
    row = ok(console, "POST", "/api/loop/step", {"measured": 45.2})
    assert row["status"] == "reverted"
    assert row["reason"] == "time limit exceeded"
    assert row["final_loads"] == {"nip1": 90.0, "nip2": 90.0, "nip3": 90.0}
    assert row["added"] == {"nip1": 0.0, "nip2": 0.0, "nip3": 0.0}
    assert services.press.load() == 45.0
    assert ok(console, "GET", "/api/loop/state")["last_status"] == "reverted"
    history = ok(console, "GET", "/api/verdict/history?subject=press.loop")
    assert history["current"] == "reverted"


def test_unreachable_target_saturates_the_nips_then_reverts(console, services):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console, target=80.0, time_limit=1, max_step=1000.0)
    row = ok(console, "POST", "/api/loop/step", {"measured": 45.0})
    assert row["loads"] == {"nip1": 600.0, "nip2": 600.0, "nip3": 600.0}
    assert row["unallocated"] > 0.0
    advance_clock(console, 2)
    row = ok(console, "POST", "/api/loop/step", {"measured": 60.0})
    assert row["status"] == "reverted"
    assert services.press.load() == 45.0


def test_operator_can_fall_back_before_the_limit(console, services):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console, target=48.0)
    ok(console, "POST", "/api/loop/step", {"measured": 45.0})
    assert services.press.load() == 70.0
    row = ok(console, "POST", "/api/loop/revert", {})
    assert row["status"] == "reverted"
    assert row["reason"] == "operator fallback"
    assert services.press.load() == 45.0


def test_step_without_a_session_is_refused(console):
    response = refused(console, "POST", "/api/loop/step", {"measured": 45.0})
    assert response.status == 409
    response = refused(console, "POST", "/api/loop/revert", {})
    assert response.status == 409


def test_second_engage_is_refused_while_a_session_runs(console):
    engage(console)
    response = refused(console, "POST", "/api/loop/engage", {"target": 47.0})
    assert response.status == 409


def test_target_outside_the_band_is_refused(console):
    response = refused(console, "POST", "/api/loop/engage", {"target": 120.0})
    assert response.status == 422
    response = refused(console, "POST", "/api/loop/step", {"measured": -1.0})
    assert response.status in (409, 422)


def test_sessions_and_steps_stay_queryable(console):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console)
    ok(console, "POST", "/api/loop/step", {"measured": 45.0})
    ok(console, "POST", "/api/loop/step", {"measured": 45.9})
    ok(console, "POST", "/api/loop/step", {"measured": 46.0})
    sessions = ok(console, "GET", "/api/loop/sessions")
    # one row for the engagement and one for the close, both against the same id
    assert sessions["count"] == 2
    final = sessions["sessions"][-1]
    assert final["status"] == "converged"
    assert final["steps"] == 3
    assert round(final["added"]["nip1"], 2) == 16.67
    steps = ok(console, "GET", "/api/loop/steps")
    assert steps["count"] == 3
    filtered = ok(console, "GET", f"/api/loop/steps?session={final['session_id']}")
    assert filtered["count"] == 3
    reverted = ok(console, "GET", "/api/loop/sessions?status=reverted")
    assert reverted["count"] == 0


def test_loop_events_land_in_the_audit_trail(console):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console, target=48.0)
    ok(console, "POST", "/api/loop/step", {"measured": 45.0})
    ok(console, "POST", "/api/loop/revert", {})
    assert ok(console, "GET", "/api/audit?topic=press.loop.engaged")["count"] == 1
    # the correcting step and the fallback step each left a record
    assert ok(console, "GET", "/api/audit?topic=press.loop.step")["count"] == 2
    assert ok(console, "GET", "/api/audit?topic=press.loop.reverted")["count"] == 1
    summary = ok(console, "GET", "/api/audit/summary?topic=press.loop.reverted")
    assert summary["topic_count"] == 1


def test_loop_state_survives_a_restart(config):
    first = build(config)
    ok(first.console, "POST", "/api/press/load", {"load": 45})
    engage(first.console)
    ok(first.console, "POST", "/api/loop/step", {"measured": 45.0})

    second = build(config)
    view = ok(second.console, "GET", "/api/loop/state")
    assert view["active"] is True
    assert round(view["loads"]["nip1"], 2) == 106.67
    # the press total follows the restored nip loads
    assert round(second.press.load(), 2) == 53.33
    # the restored session still closes against its own target
    ok(second.console, "POST", "/api/loop/step", {"measured": 45.9})
    row = ok(second.console, "POST", "/api/loop/step", {"measured": 46.0})
    assert row["status"] == "converged"
    sessions = ok(second.console, "GET", "/api/loop/sessions")
    assert sessions["count"] == 2


def test_loop_model_reports_the_arithmetic(console):
    ok(console, "POST", "/api/press/load", {"load": 45})
    engage(console)
    body = ok(console, "GET", "/api/loop/model?error=1.0")
    assert body["correction"] == 50.0
    assert body["gain"] == 0.02
    assert len(body["nips"]) == 3
    assert round(sum(body["distributed"]), 2) == 50.0


def test_correction_turns_error_into_load():
    assert calc.correction(1.0, 0.02) == 50.0
    with pytest.raises(ValueError):
        calc.correction(1.0, 0.0)


def test_settled_needs_a_run_inside_the_tolerance():
    assert calc.settled([0.4, 0.3], 0.5, 2) is True
    assert calc.settled([0.9, 0.3], 0.5, 2) is False
    assert calc.settled([0.3], 0.5, 2) is False
    with pytest.raises(ValueError):
        calc.settled([0.3], 0.5, 0)


def test_distribute_obeys_the_per_step_rate_limit():
    change = calc.distribute(300.0, [0.0] * 3, [0.0] * 3, [600.0] * 3, [1.0] * 3, 50.0)
    assert change == [50.0, 50.0, 50.0]


def test_distribute_caps_at_headroom_and_moves_the_rest():
    change = calc.distribute(150.0, [590.0, 0.0, 0.0], [0.0] * 3, [600.0] * 3, [1.0] * 3, 500.0)
    assert change[0] == pytest.approx(10.0)
    assert change[1] + change[2] == pytest.approx(140.0)


def test_distribute_weights_the_shares():
    change = calc.distribute(100.0, [0.0, 0.0], [0.0, 0.0], [600.0, 600.0], [3.0, 1.0], 500.0)
    assert change[0] == pytest.approx(75.0)
    assert change[1] == pytest.approx(25.0)


def test_distribute_can_back_load_off():
    change = calc.distribute(-60.0, [100.0, 100.0], [20.0, 20.0], [600.0, 600.0], [1.0, 1.0], 500.0)
    assert change[0] == pytest.approx(-30.0)
    assert change[1] == pytest.approx(-30.0)
