"""Per-session and per-slot overrides + satisfied-interest correctness."""
from sqlalchemy import select
from app.types import Problem, Candidate, Slot, WEIGHT
from app.solver import build_and_solve

from app.models import (Event, Room, Participant, Proposal, Rating, Round,
                        Timeslot, TimeslotRoom, Placement)
from app.services import proposals as P
from app.services import ratings as RT
from app.services import rounds as RD
from app.solver_input import build_problem


# ---- solver: satisfied interest + must_run (pure) --------------------------

def test_total_interest_is_sum_of_seated_weights():
    # two people want C100 (5★ and 4★) in one room with capacity 2 -> both seated
    p = Problem(slots=[Slot(id=1, position=0, rooms={10: 5})],
                candidates=[Candidate(id=100, runners=frozenset())],
                present=frozenset({1, 2}), stars={(1, 100): 5, (2, 100): 4},
                lam=0.0, min_interest=1)
    r = build_and_solve(p)
    assert r.total_interest == WEIGHT[5] + WEIGHT[4]   # 9 + 4 = 13


def test_total_interest_counts_only_seated_when_over_capacity():
    # 3 want it (5★ each), room holds 1 -> only one seat counts
    p = Problem(slots=[Slot(id=1, position=0, rooms={10: 1})],
                candidates=[Candidate(id=100, runners=frozenset())],
                present=frozenset({1, 2, 3}), stars={(u, 100): 5 for u in (1, 2, 3)},
                lam=0.0, min_interest=1)
    r = build_and_solve(p)
    assert r.total_interest == WEIGHT[5]               # only 1 seat


def test_no_raters_means_zero_present_denominator():
    # nobody engaged -> n_present 0, so coverage% is not misleadingly "100%"
    p = Problem(slots=[Slot(id=1, position=0, rooms={10: 5})],
                candidates=[Candidate(id=100, runners=frozenset(), must_run=True)],
                present=frozenset(), stars={}, lam=0.0, min_interest=1)
    r = build_and_solve(p)
    assert r.n_present == 0


def test_repeatable_session_with_present_runner_is_feasible():
    # regression: a repeatable session with a present runner used to force a Bool==2
    # constraint -> spurious INFEASIBLE. Must stay feasible, <=1 room per slot.
    slots = [Slot(id=1, position=0, rooms={10: 5, 11: 5}),
             Slot(id=2, position=1, rooms={10: 5, 11: 5})]
    p = Problem(slots=slots,
                candidates=[Candidate(id=100, runners=frozenset({9}), allow_repeat=True)],
                present=frozenset({9, 1, 2}), stars={(1, 100): 5, (2, 100): 5},
                lam=0.0, min_interest=1, repeat_cap=2)
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    per_slot = {}
    for pl in r.placements:
        per_slot[pl.slot_id] = per_slot.get(pl.slot_id, 0) + 1
    assert all(v <= 1 for v in per_slot.values())   # at most one room per slot


def test_runnerless_repeatable_can_fill_two_rooms_in_one_slot():
    # 4 people want a runner-less repeatable session; two rooms cap 2 each, one slot.
    # It should run in BOTH rooms (coverage), not just one (the old placed_in<=1 cap).
    p = Problem(slots=[Slot(id=1, position=0, rooms={10: 2, 11: 2})],
                candidates=[Candidate(id=100, runners=frozenset(), allow_repeat=True)],
                present=frozenset({1, 2, 3, 4}), stars={(u, 100): 5 for u in (1, 2, 3, 4)},
                lam=12.0, min_interest=1, repeat_cap=2)
    r = build_and_solve(p)
    rooms_used = {pl.room_id for pl in r.placements if pl.candidate_id == 100}
    assert rooms_used == {10, 11}                 # filled both rooms this slot


def test_evaluate_current_scores_force_placed_ineligible_session(session):
    # a session below the interest bar, force-placed, must still be scored (not dropped)
    s = session; e = _evt(s)
    room = Room(event_id=e.id, name="H", capacity=10); s.add(room); s.flush()
    rnd = RD.create_round(s, event_id=e.id, name="R", min_interest=5)   # high bar
    ts = RD.add_timeslot(s, round_id=rnd.id, name="A", position=0, room_ids=[room.id])
    u = _p(s, e, "U")
    a = P.propose(s, event_id=e.id, title="X", created_by=u.id, runner=False)
    RT.set_rating(s, participant_id=u.id, proposal_id=a.id, stars=5)    # 1 interested < 5
    RD.force_place(s, timeslot_id=ts.id, room_id=room.id, proposal_id=a.id)
    r = RD.evaluate_current(s, rnd.id)
    assert any(pl.candidate_id == a.id for pl in r.placements)


def test_must_run_places_even_with_no_interest():
    # nobody is interested, but must_run forces it onto the grid
    p = Problem(slots=[Slot(id=1, position=0, rooms={10: 5})],
                candidates=[Candidate(id=100, runners=frozenset(), must_run=True)],
                present=frozenset(), stars={}, lam=0.0, min_interest=1)
    r = build_and_solve(p)
    assert {pl.candidate_id for pl in r.placements} == {100}


# ---- solver_input: config plumbing (DB) ------------------------------------

def _evt(s):
    e = Event(name="R", participant_code="p", admin_code="a"); s.add(e); s.flush()
    return e

def _p(s, e, n):
    u = Participant(event_id=e.id, display_name=n); s.add(u); s.flush(); return u


def test_evaluate_current_scores_the_actual_grid_not_a_resolve(session):
    s = session; e = _evt(s)
    room = Room(event_id=e.id, name="Lib", capacity=10); s.add(room); s.flush()
    rnd = RD.create_round(s, event_id=e.id, name="R")
    ts = RD.add_timeslot(s, round_id=rnd.id, name="14:00", position=0, room_ids=[room.id])
    u = _p(s, e, "Al")
    a = P.propose(s, event_id=e.id, title="AI", created_by=u.id)
    RT.set_rating(s, participant_id=u.id, proposal_id=a.id, stars=5)
    # nothing placed yet -> the grid is empty, so the rater is NOT covered (no 100%)
    r = RD.evaluate_current(s, rnd.id)
    assert r.placements == []
    assert r.coverage_pct[ts.id] == 0.0
    assert u.id in r.left_out[ts.id]
    # after solving+applying, the same evaluation reflects the real placement
    RD.apply_result(s, rnd.id, RD.solve_round(s, rnd.id))
    r2 = RD.evaluate_current(s, rnd.id)
    assert r2.placements and r2.coverage_pct[ts.id] == 100.0


def test_per_slot_capacity_override(session):
    s = session; e = _evt(s)
    room = Room(event_id=e.id, name="Hall", capacity=10); s.add(room); s.flush()
    rnd = RD.create_round(s, event_id=e.id, name="R1")
    ts = RD.add_timeslot(s, round_id=rnd.id, name="A", position=0, room_ids=[room.id])
    RD.set_timeslot_rooms(s, timeslot_id=ts.id, room_ids=[room.id], caps={room.id: 3})
    prob = build_problem(s, rnd.id)
    assert prob.slots[0].rooms[room.id] == 3            # override beats room default 10


def test_min_interest_override_and_must_run(session):
    s = session; e = _evt(s)
    room = Room(event_id=e.id, name="Hall", capacity=10); s.add(room); s.flush()
    rnd = RD.create_round(s, event_id=e.id, name="R1", min_interest=3)
    RD.add_timeslot(s, round_id=rnd.id, name="A", position=0, room_ids=[room.id])
    u = _p(s, e, "U1")
    a = P.propose(s, event_id=e.id, title="A", created_by=u.id, runner=False)
    RT.set_rating(s, participant_id=u.id, proposal_id=a.id, stars=5)  # only 1 interested
    # default min_interest=3 -> excluded
    assert all(c.id != a.id for c in build_problem(s, rnd.id).candidates)
    # per-session override to 1 -> included
    P.set_config(s, a.id, min_interest_override=1)
    assert any(c.id == a.id for c in build_problem(s, rnd.id).candidates)


def test_min_minutes_derives_multislot(session):
    s = session; e = _evt(s)
    room = Room(event_id=e.id, name="Hall", capacity=10); s.add(room); s.flush()
    rnd = RD.create_round(s, event_id=e.id, name="R1")
    RD.add_timeslot(s, round_id=rnd.id, name="A", position=0, duration_minutes=45, room_ids=[room.id])
    RD.add_timeslot(s, round_id=rnd.id, name="B", position=1, duration_minutes=45, room_ids=[room.id])
    u = _p(s, e, "U1")
    a = P.propose(s, event_id=e.id, title="Long", created_by=u.id)
    RT.set_rating(s, participant_id=u.id, proposal_id=a.id, stars=5)
    P.set_config(s, a.id, min_minutes=90)               # 90 / 45 = 2 slots
    cand = next(c for c in build_problem(s, rnd.id).candidates if c.id == a.id)
    assert cand.slots_needed == 2


def test_must_run_bonus_does_not_farm_cells():
    # A must_run + allow_repeat session with low interest must NOT grab every room
    # to bank the bonus per cell — it runs once, leaving the other room for the
    # high-interest session. (Regression: per-cell bonus filled the whole grid.)
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 5, 11: 5})],
        candidates=[
            Candidate(id=100, runners=frozenset(), allow_repeat=True, must_run=True),
            Candidate(id=200, runners=frozenset()),
        ],
        present=frozenset({1, 2, 3, 4, 5, 6}),
        stars={**{(1, 100): 3},                              # must_run: 1 weak voter
               **{(u, 200): 5 for u in (2, 3, 4, 5, 6)}},    # rival: lots of demand
        lam=0.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    runs_100 = [pl for pl in r.placements if pl.candidate_id == 100]
    runs_200 = [pl for pl in r.placements if pl.candidate_id == 200]
    assert len(runs_100) == 1            # ran once, didn't farm both rooms
    assert len(runs_200) >= 1            # rival session still got a room


def test_elsewhere_session_persists_roomless_and_renders(session):
    s = session; e = _evt(s)
    room = Room(event_id=e.id, name="Lib", capacity=10); s.add(room); s.flush()
    rnd = RD.create_round(s, event_id=e.id, name="R")
    ts = RD.add_timeslot(s, round_id=rnd.id, name="14:00", position=0, room_ids=[room.id])
    u = _p(s, e, "Al"); v = _p(s, e, "Bo")
    # a normal (room) session and an elsewhere (no-room) session
    room_sess = P.propose(s, event_id=e.id, title="Indoor talk", created_by=u.id, runner=False)
    walk = P.propose(s, event_id=e.id, title="Walk & talk", created_by=v.id,
                     runner=False, needs_space=False)
    for p in (room_sess, walk):
        RT.set_rating(s, participant_id=u.id, proposal_id=p.id, stars=5)
        RT.set_rating(s, participant_id=v.id, proposal_id=p.id, stars=5)
    RD.apply_result(s, rnd.id, RD.solve_round(s, rnd.id))
    pls = {pl.proposal_id: pl for pl in s.scalars(
        select(Placement).where(Placement.timeslot_id == ts.id))}
    assert pls[room_sess.id].room_id == room.id      # normal session takes the room
    assert pls[walk.id].room_id is None              # elsewhere session is roomless
    md = RD.schedule_markdown(s, rnd.id)
    assert "Elsewhere: Walk & talk" in md
    assert "Lib: Indoor talk" in md


def test_regenerate_pins_and_preserves_started_sessions(session):
    from datetime import datetime, timezone
    s = session; e = _evt(s)
    room = Room(event_id=e.id, name="Lib", capacity=10); s.add(room); s.flush()
    rnd = RD.create_round(s, event_id=e.id, name="R")
    ts1 = RD.add_timeslot(s, round_id=rnd.id, name="A", position=0, room_ids=[room.id])
    RD.add_timeslot(s, round_id=rnd.id, name="B", position=1, room_ids=[room.id])
    u = _p(s, e, "Al")
    a = P.propose(s, event_id=e.id, title="Talk", created_by=u.id, runner=False)
    RT.set_rating(s, participant_id=u.id, proposal_id=a.id, stars=5)
    RD.apply_result(s, rnd.id, RD.solve_round(s, rnd.id))
    placed = s.scalar(select(Placement).where(Placement.proposal_id == a.id))
    # the session starts; pin started slots, then regenerate
    s.get(Timeslot, placed.timeslot_id).started_at = datetime.now(timezone.utc); s.flush()
    started_slot = placed.timeslot_id
    n = RD.pin_started_placements(s, rnd.id)
    assert n == 1
    RD.apply_result(s, rnd.id, RD.solve_round(s, rnd.id))
    after = s.scalars(select(Placement).where(Placement.timeslot_id == started_slot)).all()
    assert len(after) == 1 and after[0].proposal_id == a.id and after[0].pinned is True
