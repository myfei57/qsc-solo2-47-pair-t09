"""Closed-loop dryness control: per-nip allocation, timed fallback and the record."""

from support import advance_clock, build, issue_confirmation, ok, refused


def set_target(console, target=45.0, limit_ticks=None):
    body = {"target": target}
    if limit_ticks is not None:
        body["limit_ticks"] = limit_ticks
    return ok(console, "POST", "/api/pressloop/target", body)


def run_cycle(console, measured):
    return ok(console, "POST", "/api/pressloop/cycle", {"measured": measured})


def drive_to_convergence(console, measured, steps=20):
    """Feed the loop readings that follow its own achieved correction."""
    row = None
    for _ in range(steps):
        row = run_cycle(console, measured)
        if row["verdict"] == "converged":
            return row, measured
        measured += row["achieved"]
    return row, measured


def nip_loads(console):
    state = ok(console, "GET", "/api/pressloop/state")
    return {nip["name"]: nip["load"] for nip in state["nips"]}


def test_each_nip_takes_its_weighted_share_of_the_correction(console):
    set_target(console, 45.0)
    row = run_cycle(console, 43.0)
    assert row["verdict"] == "adjusting"
    assert row["error"] == 2.0
    # each nip wants its share of 2.0 dryness points but the per-cycle step caps it
    assert [move["delta"] for move in row["moves"]] == [8.0, 8.0, 8.0]
    assert [move["nip"] for move in row["moves"]] == ["nip1", "nip2", "nip3"]
    assert round(row["achieved"], 2) == 0.48
    assert nip_loads(console) == {"nip1": 48.0, "nip2": 58.0, "nip3": 68.0}


def test_convergence_updates_the_steady_state_anchor(console, services):
    set_target(console, 45.0)
    row, measured = drive_to_convergence(console, 43.0)
    assert row["verdict"] == "converged"
    assert row["moves"] == []
    loads = nip_loads(console)
    state = ok(console, "GET", "/api/pressloop/state")
    assert state["status"] == "converged"
    assert state["anchor"] == loads
    assert state["measured"] == measured
    assert services.pressloop.history().current() == "converged"


def test_fallback_returns_to_the_anchor_inside_the_limit(console):
    set_target(console, 45.0)
    drive_to_convergence(console, 43.0)
    anchor = nip_loads(console)

    # speed up: the sheet gets wetter and the reading stops answering
    set_target(console, 50.0, limit_ticks=3)
    first = run_cycle(console, 46.0)
    assert first["verdict"] == "adjusting"
    assert nip_loads(console) != anchor
    advance_clock(console, 4)

    row = run_cycle(console, 46.0)
    assert row["verdict"] == "fallback"
    assert row["reason"] == "deadline exceeded"
    assert [move["delta"] for move in row["moves"]] == [-8.0, -8.0, -8.0]
    assert nip_loads(console) == anchor
    assert ok(console, "GET", "/api/pressloop/state")["status"] == "fallback"

    # the loop stays put until the operator re-arms it
    assert refused(console, "POST", "/api/pressloop/cycle", {"measured": 46.0}).status == 409


def test_drift_after_convergence_opens_a_fresh_window(console):
    set_target(console, 45.0)
    drive_to_convergence(console, 43.0)

    row = run_cycle(console, 44.0)
    assert row["verdict"] == "adjusting"
    assert row["episode"] == 2
    state = ok(console, "GET", "/api/pressloop/state")
    assert state["status"] == "adjusting"
    assert state["deadline_tick"] == row["deadline_tick"]


def test_resume_rearms_the_loop_after_a_fallback(console):
    set_target(console, 45.0, limit_ticks=2)
    run_cycle(console, 40.0)
    advance_clock(console, 3)
    assert run_cycle(console, 40.0)["verdict"] == "fallback"

    resumed = ok(console, "POST", "/api/pressloop/resume", {})
    assert resumed["status"] == "adjusting"
    assert resumed["episode"] == 2
    assert run_cycle(console, 44.8)["verdict"] == "converged"


def test_every_cycle_is_on_record_for_later_review(console):
    set_target(console, 45.0)
    drive_to_convergence(console, 43.0)

    body = ok(console, "GET", "/api/pressloop/cycles")
    assert body["count"] == 5
    first = body["cycles"][0]
    assert first["verdict"] == "adjusting"
    assert first["moves"][0] == {"nip": "nip1", "before": 40.0, "delta": 8.0, "after": 48.0}
    assert body["cycles"][-1]["verdict"] == "converged"
    assert body["history"]["current"] == "converged"

    audit = ok(console, "GET", "/api/audit?topic=pressloop.cycle.recorded")
    assert audit["count"] == body["count"]
    verdicts = ok(console, "GET", "/api/verdict/history?subject=pressloop.convergence")
    assert verdicts["current"] == "converged"


def test_fallback_is_on_record_for_later_review(console):
    set_target(console, 45.0, limit_ticks=2)
    run_cycle(console, 40.0)
    advance_clock(console, 3)
    run_cycle(console, 40.0)

    cycles = ok(console, "GET", "/api/pressloop/cycles")["cycles"]
    assert [row["verdict"] for row in cycles] == ["adjusting", "fallback"]
    audit = ok(console, "GET", "/api/audit?topic=pressloop.fallback")
    assert audit["count"] == 1
    assert audit["records"][0]["payload"]["cycle_id"] == cycles[-1]["cycle_id"]


def test_cycle_without_a_target_is_refused(console):
    assert refused(console, "POST", "/api/pressloop/cycle", {"measured": 43.0}).status == 409
    assert refused(console, "POST", "/api/pressloop/resume", {}).status == 409


def test_model_previews_the_moves_without_applying_them(console):
    set_target(console, 45.0)
    body = ok(console, "GET", "/api/pressloop/model?measured=43")
    assert body["converged"] is False
    assert [move["delta"] for move in body["moves"]] == [8.0, 8.0, 8.0]
    assert round(body["achieved"], 2) == 0.48
    assert nip_loads(console) == {"nip1": 40.0, "nip2": 50.0, "nip3": 60.0}


def test_configure_replaces_the_nip_table_under_a_confirmation(console):
    nips = [
        {"name": "nip1", "load": 30.0, "min_load": 0.0, "max_load": 80.0, "weight": 1.0, "gain": 0.03},
        {"name": "nip2", "load": 40.0, "min_load": 0.0, "max_load": 80.0, "weight": 2.0, "gain": 0.03},
    ]
    assert refused(console, "POST", "/api/pressloop/configure", {"nips": nips, "confirmation_id": "cfm-000001"}).status == 404

    confirmation = issue_confirmation(console, "pressloop.config", "pressloop")
    row = ok(
        console,
        "POST",
        "/api/pressloop/configure",
        {"nips": nips, "tolerance": 0.2, "limit_ticks": 6, "max_step": 5.0, "confirmation_id": confirmation},
    )
    assert row["generation"] == 1
    state = ok(console, "GET", "/api/pressloop/state")
    assert [nip["name"] for nip in state["nips"]] == ["nip1", "nip2"]
    assert state["tolerance"] == 0.2
    assert state["generation"] == 1
    # a new configuration retires the old target: the loop is idle again
    assert state["status"] == "idle"
    assert refused(console, "POST", "/api/pressloop/cycle", {"measured": 43.0}).status == 409


def test_loop_state_survives_a_restart(config):
    first = build(config)
    set_target(first.console, 45.0)
    drive_to_convergence(first.console, 43.0)
    loads = nip_loads(first.console)

    second = build(config)
    state = ok(second.console, "GET", "/api/pressloop/state")
    assert state["status"] == "converged"
    assert state["target"] == 45.0
    assert state["anchor"] == loads
    assert {nip["name"]: nip["load"] for nip in state["nips"]} == loads
    assert ok(second.console, "GET", "/api/pressloop/cycles")["count"] == 5


def test_pure_allocation_arithmetic():
    from paperops.pressloop import calc

    nips = [
        {"name": "a", "load": 10.0, "min_load": 0.0, "max_load": 100.0, "weight": 1.0, "gain": 0.02},
        {"name": "b", "load": 96.0, "min_load": 0.0, "max_load": 100.0, "weight": 3.0, "gain": 0.02},
    ]
    assert calc.shares([1.0, 3.0]) == (0.25, 0.75)
    moves = calc.moves(2.0, nips, 8.0)
    # b wants 75 kN/m but only 4 remain up to its limit
    assert [move["delta"] for move in moves] == [8.0, 4.0]
    assert round(calc.achieved(moves, nips), 2) == 0.24
    assert calc.converged(0.2, 0.3) is True
    assert calc.converged(-0.4, 0.3) is False
