from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from app.types import WEIGHT, INTERESTED_STARS, ELSEWHERE, Problem, Result, PlacementOut

MAX_TIME_S = 2.0


def _weight(problem: Problem, u: int, cid: int, runners: frozenset[int]) -> int:
    if u in runners:
        return WEIGHT[5]
    return WEIGHT.get(problem.stars.get((u, cid), 0), 0)


def build_and_solve(problem: Problem) -> Result:
    m = cp_model.CpModel()
    slots = sorted(problem.slots, key=lambda s: s.position)
    n = len(slots)
    idx = {s.id: i for i, s in enumerate(slots)}
    runner_of = {c.id: c.runners for c in problem.candidates}

    # For each candidate, enumerate valid (start_index, room) placements and, for
    # each, the slot ids the span covers. A start is valid iff the whole span fits
    # and the room is available in every covered slot.
    def spans(c):
        out = []  # (start_slot_id, room_id, covered_slot_ids)
        for i in range(n - c.slots_needed + 1):
            covered = slots[i:i + c.slots_needed]
            if not c.needs_space:
                # no room required — placeable in any slot span that fits (ELSEWHERE)
                out.append((covered[0].id, ELSEWHERE, [s.id for s in covered]))
                continue
            common_rooms = set(covered[0].rooms)
            for s in covered[1:]:
                common_rooms &= set(s.rooms)
            for r in common_rooms:
                out.append((covered[0].id, r, [s.id for s in covered]))
        return out

    place: dict[tuple[int, int, int], cp_model.IntVar] = {}  # (cid, start_slot, room)
    covers: dict[tuple[int, int, int], list[int]] = {}
    cand_covered_slots: dict[int, set[int]] = {c.id: set() for c in problem.candidates}
    for c in problem.candidates:
        for (start_id, room_id, covered_ids) in spans(c):
            v = m.NewBoolVar(f"place_{c.id}_{start_id}_{room_id}")
            place[(c.id, start_id, room_id)] = v
            covers[(c.id, start_id, room_id)] = covered_ids
            cand_covered_slots[c.id].update(covered_ids)

    # runs_in[(cid, slot, room)] = sum of starts (of that candidate/room) covering slot
    def runs_in(cid, slot_id, room_id):
        terms = [place[(cid, start_id, r)]
                 for (ccid, start_id, r), cov in covers.items()
                 if ccid == cid and r == room_id and slot_id in cov]
        return sum(terms) if terms else 0

    def placed_in(cid, slot_id):
        terms = [place[(ccid, start_id, r)]
                 for (ccid, start_id, r), cov in covers.items()
                 if ccid == cid and slot_id in cov]
        return sum(terms) if terms else 0

    # fixed-grid mode: freeze every placement var to the given grid (score as-is)
    if problem.fixed is not None:
        for key, var in place.items():
            m.Add(var == (1 if key in problem.fixed else 0))

    # run-count cap
    for c in problem.candidates:
        runs = [v for (cid, _, _), v in place.items() if cid == c.id]
        if runs:
            m.Add(sum(runs) <= (problem.repeat_cap if c.allow_repeat else 1))

    # one session per (room, slot) — account multi-slot occupancy
    for s in slots:
        for room_id in s.rooms:
            occupying = [place[k] for k, cov in covers.items()
                         if k[2] == room_id and s.id in cov]
            if occupying:
                m.Add(sum(occupying) <= 1)

    # A candidate with a PRESENT runner runs in at most one room per slot (the
    # runner can't be in two rooms at once; also keeps their forced attend boolean).
    # Runner-less repeatable sessions may fill multiple rooms in a slot (coverage).
    for c in problem.candidates:
        if not (runner_of[c.id] & problem.present):
            continue
        for s in slots:
            pin = placed_in(c.id, s.id)
            if not isinstance(pin, int):
                m.Add(pin <= 1)

    # attend[(u, cid, slot)] gated: interested (>=3) or runner, only on slots the
    # candidate can cover, only for present participants.
    attend: dict[tuple[int, int, int], cp_model.IntVar] = {}
    for c in problem.candidates:
        for u in problem.present:
            interested = problem.stars.get((u, c.id), 0) >= INTERESTED_STARS
            is_runner = u in runner_of[c.id]
            if not (interested or is_runner):
                continue
            for slot_id in cand_covered_slots[c.id]:
                v = m.NewBoolVar(f"att_{u}_{c.id}_{slot_id}")
                attend[(u, c.id, slot_id)] = v
                p_in = placed_in(c.id, slot_id)
                if is_runner:
                    m.Add(v == p_in)   # runner present exactly when their session runs
                else:
                    m.Add(v <= p_in)

    # one place per person per slot
    for u in problem.present:
        for s in slots:
            here = [attend[(u, c.id, s.id)] for c in problem.candidates
                    if (u, c.id, s.id) in attend]
            if here:
                m.Add(sum(here) <= 1)

    # hard seat cap (non-runners) so coverage cannot exceed the placed room's seats
    for c in problem.candidates:
        if not c.needs_space:
            continue   # elsewhere sessions have no room cap (only the host max, below)
        for s in slots:
            non_runner = [attend[(u, c.id, s.id)] for u in problem.present
                          if (u, c.id, s.id) in attend and u not in runner_of[c.id]]
            if not non_runner:
                continue
            cap_sum = sum(problem.rooms_for(s.id)[r] * runs_in(c.id, s.id, r)
                          for r in problem.rooms_for(s.id))
            m.Add(sum(non_runner) <= cap_sum)

    # host's per-session seat cap: never *seat* (count as attending) more people than
    # the host allows — effective cap = min(room seats, this). We don't penalize a
    # popular session for being capped; the overflow people are routed to their
    # next-best session by the coverage objective. Runners always fit, so a too-small
    # cap can never make a session impossible to place.
    for c in problem.candidates:
        if c.max_attendees is None:
            continue
        n_runners = sum(1 for u in problem.present if u in runner_of[c.id])
        cap = max(c.max_attendees, n_runners)
        for s in slots:
            here = [attend[(u, c.id, s.id)] for u in problem.present
                    if (u, c.id, s.id) in attend]
            if here:
                m.Add(sum(here) <= cap)

    # uncovered[(u, slot)] >= 1 - sum_c attend
    uncovered: dict[tuple[int, int], cp_model.IntVar] = {}
    for u in problem.present:
        for s in slots:
            uv = m.NewBoolVar(f"unc_{u}_{s.id}")
            uncovered[(u, s.id)] = uv
            here = [attend[(u, c.id, s.id)] for c in problem.candidates
                    if (u, c.id, s.id) in attend]
            m.Add(uv >= 1 - sum(here)) if here else m.Add(uv == 1)

    # pins (validated): fix place==1 where the key exists, else report conflict
    pin_conflicts: list[tuple[int, int, int]] = []
    for (cid, sid, rid) in problem.pins:
        if (cid, sid, rid) in place:
            m.Add(place[(cid, sid, rid)] == 1)
        else:
            pin_conflicts.append((cid, sid, rid))

    # objective: satisfied weighted interest - lambda * uncovered + force-run bonus.
    # MUST_RUN_BONUS is large so a must-run session is placed whenever a slot is
    # free, but it's a bonus (not a hard constraint) so the model never goes
    # infeasible if there's genuinely no room for it.
    MUST_RUN_BONUS = 100_000
    interest_terms = [_weight(problem, u, cid, runner_of[cid]) * v
                      for (u, cid, sid), v in attend.items()]
    # Reward a must-run session for running AT LEAST ONCE — not per placed cell.
    # Per-cell scaling let a must_run + allow_repeat session farm the bonus by
    # grabbing every legal room/slot, starving real coverage.
    must_run_ids = {c.id for c in problem.candidates if c.must_run}
    must_run_terms = []
    for cid in must_run_ids:
        places_c = [v for (ccid, _s, _r), v in place.items() if ccid == cid]
        if not places_c:
            continue
        ran = m.NewBoolVar(f"ran_{cid}")
        m.AddMaxEquality(ran, places_c)   # ran == 1 iff placed at least once
        must_run_terms.append(MUST_RUN_BONUS * ran)
    lam_i = int(round(problem.lam))
    m.Maximize(sum(interest_terms) - lam_i * sum(uncovered.values())
               + sum(must_run_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = MAX_TIME_S
    solver.parameters.random_seed = 1
    solver.parameters.num_search_workers = 1
    status = solver.Solve(m)
    ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    status_name = {cp_model.OPTIMAL: "OPTIMAL",
                   cp_model.FEASIBLE: "FEASIBLE"}.get(status, "INFEASIBLE")

    max_of = {c.id: c.max_attendees for c in problem.candidates}
    n_runners_of = {c.id: sum(1 for u in problem.present if u in runner_of[c.id])
                    for c in problem.candidates}

    def cell_cap(cid, slot_id, room_id):
        """Effective seats for one cell. ELSEWHERE has no room cap; the host max,
        if set, binds the TOTAL (runners always fit, matching the solver's
        `max(max_attendees, n_runners)`). 0 means uncapped."""
        if room_id == ELSEWHERE:
            return max_of.get(cid) or 0
        cap = problem.rooms_for(slot_id)[room_id]
        if max_of.get(cid) is not None:
            cap = min(cap, max(max_of[cid], n_runners_of[cid]))
        return cap

    placements: list[PlacementOut] = []
    total_interest = 0
    if ok:
        placed_cells = [(cid, s_id, r_id) for (cid, s_id, r_id), v in place.items()
                        if solver.Value(v) == 1]
        # Attendance is modeled per (user, candidate, slot) — NOT per room — so stats
        # are aggregated per (candidate, slot): one figure shown per session-in-slot,
        # never double-counted across a repeated session's rooms.
        group_cap: dict[tuple[int, int], int] = {}
        group_unlimited: dict[tuple[int, int], bool] = {}
        for (cid, start_id, room_id) in placed_cells:
            for slot_id in covers[(cid, start_id, room_id)]:
                cap = cell_cap(cid, slot_id, room_id)
                key = (cid, slot_id)
                if cap == 0 and room_id == ELSEWHERE:
                    group_unlimited[key] = True
                group_cap[key] = group_cap.get(key, 0) + cap
        for (cid, start_id, room_id) in placed_cells:
            demand = sum(1 for u in problem.present
                         if problem.stars.get((u, cid), 0) >= INTERESTED_STARS
                         or u in runner_of[cid])
            for slot_id in covers[(cid, start_id, room_id)]:
                key = (cid, slot_id)
                seated = sum(solver.Value(attend[(u, cid, slot_id)]) for u in problem.present
                             if (u, cid, slot_id) in attend)
                unlimited = group_unlimited.get(key, False)
                tcap = group_cap.get(key, 0)
                cap_display = 0 if unlimited else tcap
                overflow = 0 if (unlimited or not tcap) else max(0, demand - tcap)
                placements.append(PlacementOut(
                    candidate_id=cid, slot_id=slot_id, room_id=room_id,
                    projected_attendance=seated, capacity=cap_display, overflow=overflow))
        # satisfied interest = each person's weight for a session they attend,
        # counted ONCE even if that session spans multiple slots (no double count).
        seated = {(u, cid) for (u, cid, sid), var in attend.items()
                  if solver.Value(var) == 1}
        total_interest = sum(_weight(problem, u, cid, runner_of[cid])
                             for (u, cid) in seated)

    # % of present people who got into one of their MOST preferred (max-star) sessions
    top_choice_pct = 0.0
    if ok and problem.present:
        seated_by_user: dict[int, set[int]] = defaultdict(set)
        for (u, cid) in seated:
            seated_by_user[u].add(cid)
        best_star: dict[int, int] = {}
        top_cids: dict[int, set[int]] = defaultdict(set)
        for (u, cid), st in problem.stars.items():
            if u not in problem.present:
                continue
            if st > best_star.get(u, 0):
                best_star[u] = st
                top_cids[u] = {cid}
            elif st == best_star.get(u, 0):
                top_cids[u].add(cid)
        hits = sum(1 for u in problem.present
                   if best_star.get(u, 0) >= INTERESTED_STARS
                   and seated_by_user[u] & top_cids[u])
        top_choice_pct = round(100.0 * hits / len(problem.present), 1)

    coverage_pct: dict[int, float] = {}
    left_out: dict[int, list[int]] = {}
    forced: dict[int, list[int]] = {}
    n_present = max(1, len(problem.present))
    for s in slots:
        unc_here = sorted(u for u in problem.present
                          if ok and solver.Value(uncovered[(u, s.id)]) == 1)
        left_out[s.id] = unc_here
        coverage_pct[s.id] = round(
            100.0 * (len(problem.present) - len(unc_here)) / n_present, 1)
        placed_cids = {pl.candidate_id for pl in placements if pl.slot_id == s.id}
        forced[s.id] = sorted(
            u for u in unc_here
            if any(problem.stars.get((u, cid), 0) >= INTERESTED_STARS
                   for cid in placed_cids))

    return Result(status=status_name, placements=placements,
                  total_interest=total_interest, n_present=len(problem.present),
                  coverage_pct=coverage_pct, left_out=left_out,
                  forced_uncovered=forced, pin_conflicts=pin_conflicts,
                  top_choice_pct=top_choice_pct)
