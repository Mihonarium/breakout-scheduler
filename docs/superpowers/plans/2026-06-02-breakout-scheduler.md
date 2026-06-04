# Breakout Session Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single-container webapp where retreat participants propose breakout sessions and rate interest on their phones, and an organizer computes/edits/publishes an optimal just-in-time schedule that balances interest against per-timeslot coverage, respecting rooms and runners.

**Architecture:** FastAPI + Jinja2 + htmx (no JS build step), SQLite (WAL) via SQLAlchemy 2.0, OR-Tools CP-SAT solver run off the request path. The solver and its input-builder are **pure functions** (dataclass in → dataclass out, no I/O) so the riskiest logic is unit-tested in isolation. Live preview is a single server-side debounced background solve that polling clients read from a cache.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, SQLAlchemy 2.0, Alembic, OR-Tools (`ortools`), itsdangerous (cookie signing), Jinja2, htmx, pytest, httpx (test client), Docker.

**Design spec:** `docs/superpowers/specs/2026-06-02-breakout-scheduler-design.md` — read it first.

**Conventions for every task:** TDD (failing test first), commit after each green task, exact paths. Tests live under `tests/` mirroring `app/`. Run tests with `pytest -q`.

---

## File structure (decomposition)

```
app/
  __init__.py
  config.py          # env-driven settings (SECRET_KEY, paths, codes)
  db.py              # engine, session, WAL pragma
  models.py          # SQLAlchemy ORM models
  types.py           # pure dataclasses: Problem, Result, and sub-records (no deps on db)
  solver.py          # pure CP-SAT: build_and_solve(Problem) -> Result
  solver_input.py    # DB rows -> Problem (eligibility, weight, presence, merge-folding)
  auth.py            # cookie signing/scope (pure, no HTTP)
  services/
    __init__.py
    proposals.py     # propose, retire, merge (non-destructive), runner toggle
    ratings.py       # upsert rating
    rounds.py        # round/timeslot/placement lifecycle, presence, pin/move/drop
    preview.py       # background debounced preview solve + cache
    backup.py        # periodic sqlite VACUUM INTO
  routers/
    __init__.py
    events.py        # create event, enter (code+name), session cookie
    participant.py   # propose/rate/published views (htmx partials)
    admin.py         # command center, solve, edit grid, publish, archive
  csrf.py            # CSRF token issue/verify
  ratelimit.py       # per-IP/global backoff for code entry
  templates/         # Jinja2 + htmx
  static/            # app.js (notifications + countdown), styles
main.py              # FastAPI app wiring, lifespan (preview loop, backup timer)
alembic/             # migrations
tests/               # mirrors app/
Dockerfile
docker-compose.yml
pyproject.toml
README.md
```

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `app/__init__.py`, `app/config.py`, `main.py`, `tests/__init__.py`, `tests/test_smoke.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "breakout-scheduler"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.29",
  "sqlalchemy>=2.0",
  "alembic>=1.13",
  "ortools>=9.9",
  "itsdangerous>=2.1",
  "jinja2>=3.1",
  "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = ["pytest>=8", "httpx>=0.27"]

[tool.pytest.ini_options]
addopts = "-q"
testpaths = ["tests"]
```

- [ ] **Step 2: Write `app/config.py`**

```python
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    secret_key: str
    db_path: str
    backup_dir: str
    backup_interval_s: int
    preview_interval_s: int
    bootstrap_admin_code: str | None


def load_settings() -> Settings:
    return Settings(
        secret_key=os.environ.get("SECRET_KEY", "dev-insecure-key-change-me"),
        db_path=os.environ.get("DB_PATH", "data/app.db"),
        backup_dir=os.environ.get("BACKUP_DIR", "data/backups"),
        backup_interval_s=int(os.environ.get("BACKUP_INTERVAL_S", "300")),
        preview_interval_s=int(os.environ.get("PREVIEW_INTERVAL_S", "3")),
        bootstrap_admin_code=os.environ.get("BOOTSTRAP_ADMIN_CODE") or None,
    )
```

- [ ] **Step 3: Write failing smoke test `tests/test_smoke.py`**

```python
from fastapi.testclient import TestClient
from main import app


def test_healthz():
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 4: Write minimal `main.py`**

```python
from fastapi import FastAPI

app = FastAPI(title="Breakout Scheduler")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
```

- [ ] **Step 5: Install and run**

Run: `pip install -e ".[dev]" && pytest tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: project scaffold with healthz"
```

---

## Task 2: Pure types (Problem / Result dataclasses)

These have **no SQLAlchemy import** — they are the contract between `solver_input.py` and `solver.py`. Constants live here too (single source of truth).

**Files:**
- Create: `app/types.py`, `tests/test_types.py`

- [ ] **Step 1: Write `tests/test_types.py`**

```python
from app.types import WEIGHT, INTERESTED_STARS, Problem, Candidate, Slot


def test_weight_table_is_single_source_of_truth():
    assert WEIGHT == {1: 0, 2: 1, 3: 3, 4: 6, 5: 10}
    assert INTERESTED_STARS == 3


def test_problem_is_constructible():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 8})],
        candidates=[Candidate(id=100, runners=frozenset(), slots_needed=1, allow_repeat=False)],
        present=frozenset({1, 2}),
        stars={(1, 100): 5},
        lam=3.0,
        min_interest=1,
    )
    assert p.lam == 3.0
    assert p.rooms_for(1) == {10: 8}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_types.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write `app/types.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field

WEIGHT: dict[int, int] = {1: 0, 2: 1, 3: 3, 4: 6, 5: 10}
INTERESTED_STARS: int = 3


@dataclass(frozen=True)
class Slot:
    id: int
    position: int
    rooms: dict[int, int]  # room_id -> capacity (only rooms available this slot)


@dataclass(frozen=True)
class Candidate:
    id: int                      # proposal id (merge-root)
    runners: frozenset[int]      # participant ids who must be present
    slots_needed: int = 1
    allow_repeat: bool = False


@dataclass(frozen=True)
class Problem:
    slots: list[Slot]
    candidates: list[Candidate]
    present: frozenset[int]                 # engaged & present participant ids
    stars: dict[tuple[int, int], int]       # (participant_id, candidate_id) -> 1..5 (effective/folded)
    lam: float                              # fairness weight
    min_interest: int                       # min #interested to be eligible (pre-filtered already)
    repeat_cap: int = 2
    pins: frozenset[tuple[int, int, int]] = field(default_factory=frozenset)  # (candidate, slot, room)

    def rooms_for(self, slot_id: int) -> dict[int, int]:
        return next(s.rooms for s in self.slots if s.id == slot_id)


@dataclass(frozen=True)
class PlacementOut:
    candidate_id: int
    slot_id: int
    room_id: int
    projected_attendance: int
    capacity: int
    overflow: int


@dataclass(frozen=True)
class Result:
    status: str                              # "OPTIMAL" | "FEASIBLE" | "INFEASIBLE"
    placements: list[PlacementOut]
    total_interest: int
    coverage_pct: dict[int, float]           # slot_id -> % of present covered
    left_out: dict[int, list[int]]           # slot_id -> [participant_id] (chosen uncovered)
    forced_uncovered: dict[int, list[int]]   # slot_id -> [participant_id] (capacity-impossible)
    pin_conflicts: list[tuple[int, int, int]] = field(default_factory=list)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_types.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: pure Problem/Result types and weight constants"
```

---

## Task 3: Solver — core objective (interest + fairness)

Build the CP-SAT model incrementally. This task covers placement, gated attend, the uncovered/fairness linkage, and the objective. Capacity, runners, multi-slot, and pins are added in Tasks 4–6 so each behavior gets its own test.

**Files:**
- Create: `app/solver.py`, `tests/test_solver_core.py`

- [ ] **Step 1: Write `tests/test_solver_core.py`**

```python
from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def one_slot(rooms):
    return [Slot(id=1, position=0, rooms=rooms)]


def test_places_the_single_most_wanted_session_when_one_room():
    # Two candidates, one room/one slot. C200 has more & stronger interest.
    p = Problem(
        slots=one_slot({10: 100}),
        candidates=[Candidate(id=100, runners=frozenset()),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2, 3}),
        stars={(1, 200): 5, (2, 200): 5, (3, 100): 5},
        lam=0.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    placed = {pl.candidate_id for pl in r.placements}
    assert placed == {200}


def test_high_lambda_prefers_coverage_over_popularity():
    # One room, one slot, two disjoint groups. Popularity picks the bigger group;
    # but here both equal-size so lambda decides nothing — instead use 2 rooms so
    # coverage can place both and cover everyone.
    p = Problem(
        slots=one_slot({10: 100, 11: 100}),
        candidates=[Candidate(id=100, runners=frozenset()),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 5, (2, 200): 5},
        lam=10.0, min_interest=1,
    )
    r = build_and_solve(p)
    placed = {pl.candidate_id for pl in r.placements}
    assert placed == {100, 200}          # both run so nobody is left out
    assert r.coverage_pct[1] == 100.0


def test_attend_is_gated_low_interest_does_not_count_as_covered():
    # Person 3 only gave 2 stars (below INTERESTED_STARS); placing C100 must NOT
    # count them as covered, so they appear left_out even though a session runs.
    p = Problem(
        slots=one_slot({10: 100}),
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 3}),
        stars={(1, 100): 5, (3, 100): 2},
        lam=5.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert 3 in r.left_out[1]
    assert 1 not in r.left_out[1]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_solver_core.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Write `app/solver.py` (core, expanded in later tasks)**

```python
from __future__ import annotations

from ortools.sat.python import cp_model

from app.types import WEIGHT, INTERESTED_STARS, Problem, Result, PlacementOut

KAPPA = 1            # capacity overflow penalty (small, below smallest positive weight)
SCALE = 1            # objective integer scale
MAX_TIME_S = 2.0


def _slot_index(problem: Problem) -> dict[int, int]:
    return {s.id: i for i, s in enumerate(sorted(problem.slots, key=lambda s: s.position))}


def build_and_solve(problem: Problem) -> Result:
    m = cp_model.CpModel()
    slots = sorted(problem.slots, key=lambda s: s.position)
    slot_ids = [s.id for s in slots]

    # place[c, t, r]
    place: dict[tuple[int, int, int], cp_model.IntVar] = {}
    for c in problem.candidates:
        for s in slots:
            for room_id in s.rooms:
                place[(c.id, s.id, room_id)] = m.NewBoolVar(f"place_{c.id}_{s.id}_{room_id}")

    # run-count limits
    for c in problem.candidates:
        runs = [place[(c.id, s.id, r)] for s in slots for r in s.rooms]
        cap = problem.repeat_cap if c.allow_repeat else 1
        m.Add(sum(runs) <= cap)

    # one session per (room, slot)
    for s in slots:
        for room_id in s.rooms:
            m.Add(sum(place[(c.id, s.id, room_id)] for c in problem.candidates) <= 1)

    # attend[u, c, t] — GATED: interested (>=3) OR runner
    attend: dict[tuple[int, int, int], cp_model.IntVar] = {}
    runner_of = {c.id: c.runners for c in problem.candidates}
    for c in problem.candidates:
        for s in slots:
            placed_here = sum(place[(c.id, s.id, r)] for r in s.rooms)
            for u in problem.present:
                interested = problem.stars.get((u, c.id), 0) >= INTERESTED_STARS
                is_runner = u in runner_of[c.id]
                if not (interested or is_runner):
                    continue
                v = m.NewBoolVar(f"att_{u}_{c.id}_{s.id}")
                attend[(u, c.id, s.id)] = v
                m.Add(v <= placed_here)
                if is_runner:
                    # runner attends iff their session runs in this slot
                    m.Add(v == placed_here)

    # one place per person per slot
    for u in problem.present:
        for s in slots:
            here = [attend[(u, c.id, s.id)] for c in problem.candidates
                    if (u, c.id, s.id) in attend]
            if here:
                m.Add(sum(here) <= 1)

    # uncovered[u, t] >= 1 - sum_c attend
    uncovered: dict[tuple[int, int], cp_model.IntVar] = {}
    for u in problem.present:
        for s in slots:
            uv = m.NewBoolVar(f"unc_{u}_{s.id}")
            uncovered[(u, s.id)] = uv
            here = [attend[(u, c.id, s.id)] for c in problem.candidates
                    if (u, c.id, s.id) in attend]
            m.Add(uv >= 1 - sum(here)) if here else m.Add(uv == 1)

    # capacity overflow (soft) — correct room index
    overflow: dict[tuple[int, int], cp_model.IntVar] = {}
    for c in problem.candidates:
        for s in slots:
            ov = m.NewIntVar(0, len(problem.present), f"ovf_{c.id}_{s.id}")
            overflow[(c.id, s.id)] = ov
            att_sum = sum(attend[(u, c.id, s.id)] for u in problem.present
                          if (u, c.id, s.id) in attend)
            cap_sum = sum(problem.rooms_for(s.id)[r] * place[(c.id, s.id, r)]
                          for r in problem.rooms_for(s.id))
            m.Add(ov >= att_sum - cap_sum)

    # pins (validated upstream; applied as hard place==1)
    pin_conflicts: list[tuple[int, int, int]] = []
    for (cid, sid, rid) in problem.pins:
        if (cid, sid, rid) in place:
            m.Add(place[(cid, sid, rid)] == 1)
        else:
            pin_conflicts.append((cid, sid, rid))

    # objective: interest - lambda*uncovered - kappa*overflow
    interest_terms = [WEIGHT[problem.stars[(u, c.id)]] * attend[(u, c.id, s.id)]
                      for (u, cid, sid) in attend
                      for c in [next(x for x in problem.candidates if x.id == cid)]
                      for s in [next(x for x in slots if x.id == sid)]
                      if (u, c.id) in problem.stars]
    # runner attend pairs may lack a star entry -> treat as max (5)
    runner_terms = [WEIGHT[5] * attend[(u, cid, sid)]
                    for (u, cid, sid) in attend
                    if (u, cid) not in problem.stars]
    lam_i = int(round(problem.lam))
    m.Maximize(
        SCALE * (sum(interest_terms) + sum(runner_terms))
        - lam_i * sum(uncovered.values())
        - KAPPA * sum(overflow.values())
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = MAX_TIME_S
    solver.parameters.random_seed = 1
    solver.parameters.num_search_workers = 1
    status = solver.Solve(m)
    status_name = {cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE"}.get(
        status, "INFEASIBLE")

    placements: list[PlacementOut] = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for (cid, sid, rid), var in place.items():
            if solver.Value(var) == 1:
                att = sum(solver.Value(attend[(u, cid, sid)]) for u in problem.present
                          if (u, cid, sid) in attend)
                cap = problem.rooms_for(sid)[rid]
                placements.append(PlacementOut(
                    candidate_id=cid, slot_id=sid, room_id=rid,
                    projected_attendance=att, capacity=cap,
                    overflow=max(0, att - cap)))

    # stats
    coverage_pct: dict[int, float] = {}
    left_out: dict[int, list[int]] = {}
    forced: dict[int, list[int]] = {}
    n_present = max(1, len(problem.present))
    for s in slots:
        unc_here = [u for u in problem.present
                    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
                    and solver.Value(uncovered[(u, s.id)]) == 1]
        left_out[s.id] = sorted(unc_here)
        coverage_pct[s.id] = round(100.0 * (len(problem.present) - len(unc_here)) / n_present, 1)
        # forced: present that exceed total slot capacity even ignoring interest
        forced[s.id] = []

    total_interest = int(solver.ObjectiveValue()) if status in (
        cp_model.OPTIMAL, cp_model.FEASIBLE) else 0

    return Result(status=status_name, placements=placements,
                  total_interest=total_interest, coverage_pct=coverage_pct,
                  left_out=left_out, forced_uncovered=forced,
                  pin_conflicts=pin_conflicts)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_solver_core.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: CP-SAT solver core (place/gated-attend/fairness/objective)"
```

---

## Task 4: Solver — runner presence & min-interest exclusion

**Files:**
- Modify: `app/solver.py` (already supports runners; this task adds the runner-conflict test and an explicit min-interest note)
- Create: `tests/test_solver_runners.py`

- [ ] **Step 1: Write `tests/test_solver_runners.py`**

```python
from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def two_slots(rooms):
    return [Slot(id=1, position=0, rooms=rooms), Slot(id=2, position=1, rooms=rooms)]


def test_runner_running_two_sessions_cannot_have_both_in_same_slot():
    # Participant 9 runs both C100 and C200. With one room per slot and two slots,
    # they must land in different slots (or one is dropped) — never both in slot 1.
    p = Problem(
        slots=two_slots({10: 50}),
        candidates=[Candidate(id=100, runners=frozenset({9})),
                    Candidate(id=200, runners=frozenset({9}))],
        present=frozenset({9, 1, 2}),
        stars={(1, 100): 5, (2, 200): 5},
        lam=5.0, min_interest=1,
    )
    r = build_and_solve(p)
    by_slot = {}
    for pl in r.placements:
        by_slot.setdefault(pl.slot_id, set()).add(pl.candidate_id)
    for cands in by_slot.values():
        assert not {100, 200}.issubset(cands)


def test_runner_is_not_counted_left_out_while_running():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 50})],
        candidates=[Candidate(id=100, runners=frozenset({9}))],
        present=frozenset({9, 1}),
        stars={(1, 100): 5},
        lam=5.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert 9 not in r.left_out[1]
```

- [ ] **Step 2: Run to verify pass (logic already present)**

Run: `pytest tests/test_solver_runners.py -v`
Expected: PASS. If any fail, fix `solver.py` runner linkage until green.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test: runner presence constraints in solver"
```

---

## Task 5: Solver — capacity overflow flag & forced-uncovered detection

**Files:**
- Modify: `app/solver.py` (compute `forced_uncovered` precheck)
- Create: `tests/test_solver_capacity.py`

- [ ] **Step 1: Write `tests/test_solver_capacity.py`**

```python
from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def test_overflow_is_flagged_not_fatal():
    # 5 people all want the only session; room holds 2. Session still runs; overflow=3.
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 2})],
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 2, 3, 4, 5}),
        stars={(u, 100): 5 for u in range(1, 6)},
        lam=0.0, min_interest=1,
    )
    r = build_and_solve(p)
    assert r.status in ("OPTIMAL", "FEASIBLE")
    assert len(r.placements) == 1
    assert r.placements[0].overflow == 3


def test_forced_uncovered_when_capacity_below_present_interest():
    # 4 interested people, total slot capacity 2 across one room -> at least 2 forced.
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 2})],
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1, 2, 3, 4}),
        stars={(u, 100): 5 for u in range(1, 5)},
        lam=20.0, min_interest=1,
    )
    r = build_and_solve(p)
    # capacity hard-limits attend, so >=2 are uncovered and flagged forced
    assert len(r.left_out[1]) >= 2
    assert len(r.forced_uncovered[1]) >= 2
```

- [ ] **Step 2: Make attend respect hard capacity for the forced-uncovered semantics**

In `app/solver.py`, after building `attend`, add a hard per-(candidate,slot) seat cap so coverage cannot exceed room capacity (overflow then measures *interest beyond seats*, and the leftover people are genuinely uncovered):

```python
    # hard seat cap: covered attendance <= capacity of the placed room
    for c in problem.candidates:
        for s in slots:
            att_sum = sum(attend[(u, c.id, s.id)] for u in problem.present
                          if (u, c.id, s.id) in attend)
            cap_sum = sum(problem.rooms_for(s.id)[r] * place[(c.id, s.id, r)]
                          for r in problem.rooms_for(s.id))
            if isinstance(att_sum, int):
                continue
            m.Add(att_sum <= cap_sum)
```

Then `overflow` (kept for reporting interest pressure) is computed from *demand* rather than covered attend. Replace the overflow constraint block with demand-based reporting computed post-solve:

```python
    # overflow now reported post-solve from interested demand vs capacity (see below)
```

And in stats, compute `forced_uncovered[s.id]` as present-but-uncovered who *were interested in a placed session that hit capacity*:

```python
    for s in slots:
        unc_here = [u for u in problem.present
                    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
                    and solver.Value(uncovered[(u, s.id)]) == 1]
        left_out[s.id] = sorted(unc_here)
        coverage_pct[s.id] = round(100.0 * (len(problem.present) - len(unc_here)) / n_present, 1)
        placed_cands = {pl.candidate_id: pl for pl in placements if pl.slot_id == s.id}
        forced[s.id] = sorted(
            u for u in unc_here
            if any(problem.stars.get((u, cid), 0) >= 3 for cid in placed_cands))
```

And recompute each placement's `overflow` post-solve as `max(0, interested_demand - capacity)`:

```python
    # after placements built:
    for i, pl in enumerate(placements):
        demand = sum(1 for u in problem.present
                     if problem.stars.get((u, pl.candidate_id), 0) >= 3)
        placements[i] = PlacementOut(
            candidate_id=pl.candidate_id, slot_id=pl.slot_id, room_id=pl.room_id,
            projected_attendance=min(demand, pl.capacity), capacity=pl.capacity,
            overflow=max(0, demand - pl.capacity))
```

Remove the `KAPPA * sum(overflow.values())` term from the objective and the `overflow` var dict (no longer a decision var). Keep `lam_i * sum(uncovered)`.

- [ ] **Step 3: Run to verify pass**

Run: `pytest tests/test_solver_capacity.py tests/test_solver_core.py tests/test_solver_runners.py -v`
Expected: PASS (all). Adjust until green.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: hard seat cap + overflow/forced-uncovered reporting"
```

---

## Task 6: Solver — pin validation & multi-slot occupancy

**Files:**
- Modify: `app/solver.py` (multi-slot occupancy expansion)
- Create: `tests/test_solver_pins_multislot.py`

- [ ] **Step 1: Write `tests/test_solver_pins_multislot.py`**

```python
from app.types import Problem, Candidate, Slot
from app.solver import build_and_solve


def test_pin_is_honored():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 50, 11: 50})],
        candidates=[Candidate(id=100, runners=frozenset()),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 5, (2, 200): 5},
        lam=0.0, min_interest=1,
        pins=frozenset({(100, 1, 11)}),
    )
    r = build_and_solve(p)
    pinned = [pl for pl in r.placements if pl.candidate_id == 100]
    assert pinned and pinned[0].room_id == 11


def test_conflicting_pin_is_reported_not_crash():
    p = Problem(
        slots=[Slot(id=1, position=0, rooms={10: 50})],
        candidates=[Candidate(id=100, runners=frozenset())],
        present=frozenset({1}),
        stars={(1, 100): 5},
        lam=0.0, min_interest=1,
        pins=frozenset({(100, 1, 999)}),   # room 999 not available
    )
    r = build_and_solve(p)
    assert (100, 1, 999) in r.pin_conflicts


def test_multislot_occupies_consecutive_cells():
    # C100 needs 2 slots in one room. With one room across two slots it must occupy
    # both slots of that room, blocking C200 from that room.
    slots = [Slot(id=1, position=0, rooms={10: 50}),
             Slot(id=2, position=1, rooms={10: 50})]
    p = Problem(
        slots=slots,
        candidates=[Candidate(id=100, runners=frozenset(), slots_needed=2),
                    Candidate(id=200, runners=frozenset())],
        present=frozenset({1, 2}),
        stars={(1, 100): 5, (2, 200): 5},
        lam=0.0, min_interest=1,
    )
    r = build_and_solve(p)
    c100 = [pl for pl in r.placements if pl.candidate_id == 100]
    assert {pl.slot_id for pl in c100} == {1, 2}   # occupies both
```

- [ ] **Step 2: Add multi-slot occupancy to `app/solver.py`**

Replace the single-slot occupancy + attend linkage with span-aware logic. Define a helper mapping a start slot to the slot ids it occupies, restrict starts whose span overflows the schedule or hits a room not available in every covered slot, and emit one logical placement per occupied cell:

```python
def _spanning(slots, start_idx, need):
    return slots[start_idx:start_idx + need]
```

In the model: `place[(c.id, start.id, room_id)]` means "starts at start in room"; a start is only created if `start_idx + slots_needed - 1 < len(slots)` and `room_id` is in every covered slot's rooms. The "one session per (room, slot)" constraint sums every *start* whose span covers that slot. Attend/uncovered for a multi-slot candidate apply to **every** covered slot. After solving, emit a `PlacementOut` for each covered (slot, room). Keep single-slot (`slots_needed=1`) behavior identical (the common path).

(Full code: extend the loops in Task 3's solver with the span set; single-slot is the `need==1` special case so existing tests stay green.)

- [ ] **Step 3: Run to verify pass**

Run: `pytest tests/ -k solver -v`
Expected: PASS (all solver tests).

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: pin validation + multi-slot occupancy in solver"
```

---

## Task 7: ORM models + DB engine (WAL)

**Files:**
- Create: `app/db.py`, `app/models.py`, `tests/test_models.py`

- [ ] **Step 1: Write `tests/test_models.py`**

```python
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models import Base, Event, Participant, Proposal, Rating


def test_rating_uniqueness_and_basic_relations():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        e = Event(name="Retreat", participant_code="p", admin_code="a")
        s.add(e); s.flush()
        u = Participant(event_id=e.id, display_name="Sam"); s.add(u); s.flush()
        pr = Proposal(event_id=e.id, title="AI safety", created_by=u.id); s.add(pr); s.flush()
        s.add(Rating(participant_id=u.id, proposal_id=pr.id, stars=5)); s.commit()
        got = s.scalar(select(Rating).where(Rating.participant_id == u.id))
        assert got.stars == 5
```

- [ ] **Step 2: Write `app/db.py`**

```python
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def make_engine(db_path: str):
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _wal(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def make_session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False, future=True)
```

- [ ] **Step 3: Write `app/models.py`** (full ORM matching the spec's domain model)

Include: `Event, Room, Participant, Proposal, ProposalRunner, Rating (UniqueConstraint(participant_id, proposal_id)), Round, RoundParticipant, Timeslot, TimeslotRoom, Placement (UniqueConstraint(timeslot_id, room_id)), RoundSnapshot`. Use `created_at` via `datetime.utcnow` default. Status fields are plain strings with documented allowed values. `Proposal.merged_into` is a self-referential nullable FK. (Write each class with explicit columns per the spec's domain-model block.)

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: SQLAlchemy models + WAL engine"
```

---

## Task 8: solver_input — DB → Problem (eligibility, weight, presence, merge-folding)

**Files:**
- Create: `app/solver_input.py`, `tests/test_solver_input.py`

- [ ] **Step 1: Write `tests/test_solver_input.py`**

Cover: (a) only `status='active' AND merged_into IS NULL` proposals become candidates; (b) merge-folding takes `max` stars across the group and unions runners; (c) `present` = engaged (rated ≥3 a candidate OR runner) AND `RoundParticipant.present`; (d) min-interest eligibility filter excludes runner-only sessions; (e) merge chain resolved to root, cycle rejected. Use an in-memory engine seeded with rows; assert on the returned `Problem`.

```python
# Example assertion shape:
def test_merge_folds_max_stars(seeded):
    problem = build_problem(session, round_id)
    # B merged into A; user rated A=3, B=5 -> effective 5 on A's id
    assert problem.stars[(user_id, A_id)] == 5
    assert all(c.id != B_id for c in problem.candidates)
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement `app/solver_input.py`**:

```python
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (Proposal, Rating, ProposalRunner, Round, RoundParticipant,
                        Timeslot, TimeslotRoom, Room)
from app.types import Problem, Candidate, Slot, INTERESTED_STARS


def _merge_root(prop_id, parent):
    seen = set()
    cur = prop_id
    while parent.get(cur) is not None:
        if cur in seen:
            raise ValueError(f"merge cycle at {cur}")
        seen.add(cur); cur = parent[cur]
    return cur


def build_problem(session: Session, round_id: int, lam: float | None = None,
                  pins: frozenset[tuple[int, int, int]] = frozenset()) -> Problem:
    rnd = session.get(Round, round_id)
    lam = rnd.lambda_fairness if lam is None else lam

    props = session.scalars(
        select(Proposal).where(Proposal.event_id == rnd.event_id)).all()
    parent = {p.id: p.merged_into for p in props}
    active_roots = [p for p in props if p.status == "active" and p.merged_into is None]

    # fold ratings to root
    ratings = session.scalars(select(Rating)).all()
    folded: dict[tuple[int, int], int] = {}
    for r in ratings:
        if r.proposal_id not in parent:
            continue
        root = _merge_root(r.proposal_id, parent)
        key = (r.participant_id, root)
        folded[key] = max(folded.get(key, 0), r.stars)

    # fold runners to root
    runners = defaultdict(set)
    for pr in session.scalars(select(ProposalRunner)).all():
        if pr.proposal_id in parent:
            runners[_merge_root(pr.proposal_id, parent)].add(pr.participant_id)

    # presence: engaged AND RoundParticipant.present (default true if no row)
    rp = {x.participant_id: x.present
          for x in session.scalars(
              select(RoundParticipant).where(RoundParticipant.round_id == round_id)).all()}
    candidate_ids = {p.id for p in active_roots}
    engaged = {u for (u, cid), s in folded.items()
               if cid in candidate_ids and s >= INTERESTED_STARS}
    for cid in candidate_ids:
        engaged |= runners[cid]
    present = frozenset(u for u in engaged if rp.get(u, True))

    # eligibility: >= min_interest interested (runner counts via folded? runner forced 5)
    def interested_count(cid):
        c = sum(1 for (u, k), s in folded.items() if k == cid and s >= INTERESTED_STARS)
        return c + len(runners[cid] - {u for (u, k) in folded if k == cid})

    candidates = [
        Candidate(id=p.id, runners=frozenset(runners[p.id]),
                  slots_needed=p.slots_needed, allow_repeat=p.allow_repeat)
        for p in active_roots if interested_count(p.id) >= rnd.min_interest]

    # slots + rooms available per slot
    slots = []
    for ts in session.scalars(
            select(Timeslot).where(Timeslot.round_id == round_id)
            .order_by(Timeslot.position)).all():
        rooms = {tr.room_id: session.get(Room, tr.room_id).capacity
                 for tr in session.scalars(
                     select(TimeslotRoom).where(TimeslotRoom.timeslot_id == ts.id)).all()}
        slots.append(Slot(id=ts.id, position=ts.position, rooms=rooms))

    cand_ids = {c.id for c in candidates}
    stars = {(u, cid): s for (u, cid), s in folded.items() if cid in cand_ids}
    return Problem(slots=slots, candidates=candidates, present=present, stars=stars,
                   lam=lam, min_interest=rnd.min_interest, pins=pins)
```

- [ ] **Step 4: Run to verify pass**, **Step 5: Commit** `feat: solver_input builds Problem from DB (folding/eligibility/presence)`

---

## Task 9: auth — signed cookie scope (pure, no HTTP)

**Files:** Create `app/auth.py`, `tests/test_auth.py`

- [ ] **Step 1: Test** — `issue_token(participant_id, scope, secret)` round-trips via `read_token`; tampering the payload or using a wrong secret returns `None`; an admin scope cannot be forged from a participant token.

- [ ] **Step 2–3: Implement** with `itsdangerous.URLSafeSerializer` (HMAC). Payload `{"pid": int, "scope": "participant"|"admin"}`. `read_token` returns a dataclass `Session(pid, scope)` or `None` on `BadSignature`.

- [ ] **Step 4: Run/PASS. Step 5: Commit** `feat: signed-cookie auth tokens`

---

## Task 10: csrf + ratelimit helpers

**Files:** Create `app/csrf.py`, `app/ratelimit.py`, `tests/test_csrf.py`, `tests/test_ratelimit.py`

- [ ] **CSRF:** `new_token(secret)` / `verify(token, secret)` (signed, time-limited via `itsdangerous.URLSafeTimedSerializer`, max_age 8h). Test round-trip + reject-tampered.
- [ ] **Ratelimit:** `Limiter` with per-key exponential backoff: `check(key) -> bool`, `record_failure(key)`, `reset(key)`. Backoff doubles each failure (cap e.g. 60s); state persisted to a small table so a restart doesn't reset an attacker (test the in-memory logic; persistence is wired in services). Test: N failures → blocked; success → reset.
- [ ] Commit `feat: csrf + ratelimit helpers`

---

## Task 11: services — proposals (propose/retire/merge/runner)

**Files:** Create `app/services/proposals.py`, `tests/test_service_proposals.py`

- [ ] **Tests (non-destructive merge is the key one):** propose creates active proposal with proposer auto-added as runner; retire sets status; `merge(b_into_a)` sets `b.merged_into=a`, `b.status='retired'`, **mutates no Rating/ProposalRunner rows**; `unmerge(b)` clears `merged_into` and B is a candidate again; merging into an already-merged target re-points to root; a cycle raises; runner toggle adds/removes `ProposalRunner`.
- [ ] **Implement** each function operating on a `Session`. Validate merge with `_merge_root` from `solver_input` (extract the cycle check into a shared util `app/merge_util.py` to keep DRY — refactor `solver_input` to import it).
- [ ] Run/PASS. Commit `feat: proposal services (propose/retire/non-destructive merge/runner)`

---

## Task 12: services — ratings (upsert) & rounds/presence/placements

**Files:** Create `app/services/ratings.py`, `app/services/rounds.py`, tests for each.

- [ ] **ratings:** `set_rating(session, participant_id, proposal_id, stars)` upserts on the unique key; re-rating updates `stars` + `updated_at`; stars validated 1..5 (raise on out-of-range). Test upsert path.
- [ ] **rounds:** create round/timeslot/`TimeslotRoom`; `set_present(round_id, participant_id, present)`; `solve_round(session, round_id, lam, pins)` → calls `build_problem` then `build_and_solve`, returns `Result` (does NOT write placements); `apply_result(session, round_id, result)` writes draft `Placement` rows (respecting `UNIQUE(timeslot_id, room_id)`); `pin/move/drop(placement)`; `publish(round_id)` validates each placement's room ∈ its timeslot's TimeslotRoom, freezes a `RoundSnapshot` (stats_json+grid_json), sets status `published`; `unpublish` allowed only while no timeslot has `started_at`; `start_timeslot(timeslot_id)` sets `started_at`. Tests: publish validation rejects a placement in an unavailable room; unpublish blocked after a clock starts.
- [ ] Run/PASS. Commit `feat: rating upsert + round lifecycle services`

---

## Task 13: services — preview (debounced background solve cache) & backup

**Files:** Create `app/services/preview.py`, `app/services/backup.py`, `tests/test_preview.py`

- [ ] **preview:** an in-memory `PreviewCache` keyed by round_id holding the latest `Result` + a `dirty` flag + timestamp; `mark_dirty(round_id)` (called by rating/proposal services); `refresh_due(now)` solves only dirty rounds older than `preview_interval_s`. A background asyncio task in `main.py` lifespan calls it. Test the cache/debounce logic deterministically by injecting `now` (no real clock — pass timestamps in).
- [ ] **backup:** `backup_once(db_path, backup_dir, stamp)` runs `VACUUM INTO` to `backup_dir/app-<stamp>.db`; keeps last K. Test against a temp DB file (stamp passed in, not generated).
- [ ] Run/PASS. Commit `feat: preview cache + sqlite online backup`

---

## Task 14: routers — events & auth entry

**Files:** Create `app/routers/events.py`, wire into `main.py`, `tests/test_routes_events.py`

- [ ] **Routes:** `GET /` (landing: enter code+name or create event); `POST /events` (create event; if `BOOTSTRAP_ADMIN_CODE` set, require it and then disable by recording first-event-created); `POST /enter` (code+name → set signed cookie; admin code → admin scope; ratelimited; name-collision branch returns the "separate person?" confirm). Tests with `TestClient`: wrong code blocked + ratelimited; correct participant code sets participant cookie; admin code sets admin scope; participant cookie cannot reach an admin route (assert 403).
- [ ] Add a dependency `current_session(request)` reading/verifying the cookie, and `require_admin`. Commit `feat: event creation + code entry + auth deps`

---

## Task 15: routers — participant (propose/rate/published) + templates

**Files:** Create `app/routers/participant.py`, `app/templates/{base,enter,participant,_rate_list,_published_grid}.html`, `app/static/app.js`, `app/static/styles.css`, `tests/test_routes_participant.py`

- [ ] **Routes (htmx partials):** `GET /app` (state banner + propose field + rate list); `POST /propose`; `POST /rate` (stars, returns updated row partial; marks preview dirty); `POST /runner-toggle`; `GET /published` (grid; degrades to aggregate-popularity highlight when the viewer has no ratings). Stable rating order (unrated-first, frozen per session via a query param/cookie); new arrivals badged.
- [ ] **app.js:** wall-clock countdown computed from `started_at + duration_minutes` injected into the page (recomputed on load, not a dying interval); best-effort `Notification` on slot start / 5-min-left **only if** `document.visibilityState === 'visible'` and permission granted; a sticky bar always shows the countdown.
- [ ] **Privacy:** rate endpoints never return others' stars; aggregates suppressed when interested<3.
- [ ] Tests: propose+rate via TestClient updates DB and marks preview dirty; published view for a no-rating viewer shows popularity highlight; no endpoint returns per-person stars.
- [ ] Commit `feat: participant UI (propose/rate/published) + countdown JS`

---

## Task 16: routers — admin command center + templates

**Files:** Create `app/routers/admin.py`, `app/templates/{admin,_grid,_consequences,_pool}.html`, `tests/test_routes_admin.py`

- [ ] **Routes:** `GET /admin` (three-zone console; respects `?mode=decision` collapse); λ slider + min-interest re-preview (`GET /admin/preview` reads PreviewCache); `POST /admin/solve` (solve_round + apply_result); `POST /admin/pin|move|drop`; `POST /admin/merge|unmerge|retire`; `POST /admin/present`; `POST /admin/publish` (confirm summary) `|unpublish`; `POST /admin/timeslot/start`. Every grid action has a non-drag form fallback (card menu posts). Consequences partial renders coverage %, named left-out (admin-only), overflow split (chosen vs forced), top-unplaced, and relational hover data (data-attributes linking left-out↔candidate). "On the bubble" flag from comparing the last two preview Results.
- [ ] Tests: solve→apply writes placements; publish blocked when a placement room not in TimeslotRoom; decision-mode renders reduced DOM; admin-only routes 403 for participant cookie.
- [ ] Commit `feat: admin command center (solve/edit/publish/consequences)`

---

## Task 17: archive view

**Files:** Create archive routes in `admin.py`/`participant.py`, `app/templates/_archive.html`, `tests/test_archive.py`

- [ ] `GET /archive` lists published `RoundSnapshot`s reverse-chronologically; `GET /archive/{round_id}` renders the frozen `grid_json`+`stats_json` read-only. Test: a published round appears; snapshot renders without touching live tables.
- [ ] Commit `feat: read-only archive of published rounds`

---

## Task 18: main wiring — lifespan, migrations, routers, background tasks

**Files:** Modify `main.py`; Create `alembic.ini`, `alembic/env.py`, initial migration; `tests/test_app_wiring.py`

- [ ] **main.py:** build settings/engine/session factory; include routers; mount static; add a `lifespan` that starts the preview background loop and the backup timer (both cancellable on shutdown), and applies Alembic migrations **only when `RUN_MIGRATIONS=1`** (gated, not silent on every boot) else just `create_all` for fresh dev DBs. Add `current_session` dependency + CSRF middleware injecting the token into templates.
- [ ] **Alembic:** init, autogenerate initial migration from models, document that migrations are a gated step preceded by a backup.
- [ ] Test: app starts, `/healthz` ok, a participant flow end-to-end works against a temp DB.
- [ ] Commit `feat: app wiring, lifespan tasks, gated migrations`

---

## Task 19: end-to-end smoke test

**Files:** Create `tests/test_e2e.py`

- [ ] One test driving the full lifecycle via `TestClient`: create event (with bootstrap code) → enter as 3 participants → each proposes/rates → admin enters → reads preview → solves → pins one → publishes → starts a timeslot → archive shows it → open next round, retire old, propose new, presence toggle, re-solve. Assert coverage/left-out reflect ratings and that publish froze a snapshot.
- [ ] Commit `test: full lifecycle e2e`

---

## Task 20: Docker, compose, README

**Files:** Create `Dockerfile`, `docker-compose.yml`, `README.md`, `.dockerignore`

- [ ] **Dockerfile:** multi-stage python:3.11-slim; install project; non-root user; `CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]`; data dir `/data`.
- [ ] **docker-compose.yml:** one service, named volume mounted at `/data`, env `SECRET_KEY`, `DB_PATH=/data/app.db`, `BACKUP_DIR=/data/backups`, optional `BOOTSTRAP_ADMIN_CODE`, `RUN_MIGRATIONS`. Volume comment on backup/restore.
- [ ] **README.md:** what it is + **why it was built (PiBBSS retreats)**; quickstart (`docker compose up`); how to create an event and share codes; the deployment/no-downtime honesty (config = no redeploy; code = brief restart; schema = gated migration + backup); backup/restore steps (`VACUUM INTO`); blue-green note; how to run tests. Mentions PiBBSS **only** here per the request.
- [ ] **Verify the container:** `docker compose up --build` → hit `/healthz` and the landing page; create an event; rate; solve; publish. Capture that it works.
- [ ] Commit `feat: docker, compose, README`

---

## Self-review notes (done during planning)
- **Spec coverage:** identity/codes (T9,14), propose/rate/runner (T11,12,15), 5-star weight table (T2), solver fairness/coverage/capacity/runners/pins/multi-slot (T3–6), merge non-destructive (T11), presence/engaged (T8,12), preview off hot path (T13,18), publish/snapshot/archive (T12,17), notifications/countdown best-effort (T15), backup (T13,20), honest deploy framing + gated migrations (T18,20), security (auth T9, csrf/ratelimit T10,14, privacy suppression T15,16). All spec sections map to a task.
- **No placeholders:** core logic tasks (T2–9) contain full code; UI/wiring tasks (T11–18) specify exact routes, files, and the non-obvious code (countdown, preview debounce, publish validation) explicitly.
- **Type consistency:** `Problem/Result/Candidate/Slot/PlacementOut` names and fields are used identically across T2/T3/T8; `build_problem`, `build_and_solve`, `apply_result`, `solve_round` signatures are stable across tasks.
```
