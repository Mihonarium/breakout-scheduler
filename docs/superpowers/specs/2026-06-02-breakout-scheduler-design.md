# Breakout Session Scheduler — Design

**Date:** 2026-06-02
**Status:** Approved (design delegated to implementer; proceed to implementation)
**Revision:** v2 — incorporates three independent design reviews (solver/OR, product/UX, architecture/ops).

## Context

Retreats (this tool is built for PiBBSS retreats; see README) run **breakout
sessions**: participants propose discussion topics out loud, others say how much
they want to attend each one, and an organizer has to decide — usually *minutes
before the block starts* — which sessions actually run, in which room, in which
timeslot. Doing this on a whiteboard is slow and ignores intensity of interest,
room capacity, and the constraint that session-runners must be present at their
own session.

This app makes that just-in-time decision fast and good: collect proposals +
weighted interest on phones, then compute a schedule that maximizes interest
while making sure **almost nobody is left with nothing they'd want to attend** in
any timeslot, respecting rooms and runners. The organizer sees a live preview
(including who'd be left out) so they can verbally merge/add sessions, then
publishes.

A retreat runs **multiple breakout rounds** over its duration. The proposal pool
churns between rounds: old breakouts are retired into an archive, new ones are
proposed.

## Goals

- Propose a breakout in seconds (title only by default; description optional & de-emphasized).
- Rate interest in every proposal fast on a phone (5-star).
- Mark yourself (and co-runners) as **running** a session → must be present.
- Compute and publish a schedule that balances **total weighted interest**
  against **per-timeslot coverage** (minimize *engaged* people with nothing they'd
  want to attend), respecting room capacity (as a flag) and runner constraints.
- Organizer can **pin/move/drop** placements and **re-solve** around pins.
- Run **many rounds** across a retreat; retire old breakouts; browse an archive.
- A **glanceable, projector-friendly published grid** is the primary "where do I
  go" artifact. In-browser notifications are a *best-effort* enhancement (see
  Notifications — they cannot be relied on when phones are locked).
- Deploy as **one Docker container** with a SQLite volume; **config changes
  (events/rooms/rounds/proposals) need no redeploy**; a *code* change is a brief
  (~1s, no migration) container restart with data preserved.

## Non-goals (v1)

- Email / SMS / web-push-when-tab-closed (no outbound messaging; no service
  worker push). Notifications fire only while the tab is foregrounded.
- Accounts/passwords (identity is name + shared code).
- Per-person hard assignment ("you must attend X"). The schedule places
  *sessions*; people self-select on the day. Per-person attendance is an
  **optimizer estimate** used only to score coverage and capacity — never a
  prediction or instruction (see Solver note).
- Cryptographic rating privacy. Ratings are **not exposed in any UI**, but the
  organizer has DB access; the honest-rating incentive is social, not enforced.

## Personas & access model

- **Participant** — enters the **event participant code** + a display name. Can
  propose, rate, mark themselves running, view the published grid, get
  best-effort notifications. Never sees anyone's individual ratings.
- **Organizer/Admin** — enters the **event admin code**. Everything a participant
  can, plus: configure rooms, create/configure rounds + timeslots, run the
  solver, edit/pin/drop placements, set fairness λ and min-interest, retire/merge
  proposals, mark participants present/absent, merge duplicate participants, start
  timeslot clocks, publish/unpublish, and create events.

Identity is **name + code**, persisted via a **signed, `HttpOnly`, `Secure`,
`SameSite=Lax` cookie** whose payload contains `participant_id` *and* the scope
(`participant`|`admin`) — the scope is inside the signed blob, never a separate
unsigned flag, so the participant code can never yield admin rights. Returning
users are recognized by cookie first; a name collision is the rare branch
("This will create a *separate* person named Alex — continue?") with a clear
warning, and the organizer has a **merge-participants** recovery action for the
dead-phone/new-device case.

## Domain model

```
Event(id, name, participant_code, admin_code, created_at)
Room(id, event_id, name, capacity)
Participant(id, event_id, display_name, created_at)
  # cookie holds participant_id; (event_id, display_name) intentionally NOT unique

Proposal(id, event_id, title, description?, status['active'|'retired'],
         created_by → Participant, slots_needed=1, allow_repeat=false,
         merged_into → Proposal?, created_at)
ProposalRunner(proposal_id, participant_id)            # required-present people
Rating(participant_id, proposal_id, stars 1..5, updated_at)
  # UNIQUE(participant_id, proposal_id) — upsert on re-rate

Round(id, event_id, name, status['draft'|'collecting'|'published'|'archived'],
      lambda_fairness, min_interest, created_at, published_at?)
RoundParticipant(round_id, participant_id, present bool default true)
  # organizer can flag someone absent for THIS round (drops runner constraint)
Timeslot(id, round_id, name, position, duration_minutes, started_at?)
TimeslotRoom(timeslot_id, room_id)                     # availability ⇒ max parallel
Placement(id, timeslot_id, room_id, proposal_id, span=1, pinned bool)
  # round_id derived via timeslot (NOT stored — avoids inconsistency)
  # UNIQUE(timeslot_id, room_id) — DB-enforced single occupancy per cell
RoundSnapshot(round_id, stats_json, grid_json, published_at)   # frozen archive (display-only)
```

Integrity rules (DB constraints + service validation):
- **Rooms are event-level**, reused across rounds. A timeslot exposes a subset of
  rooms via `TimeslotRoom`; the number of parallel sessions varies per timeslot
  and capacities can differ. A `Placement`'s `room_id` **must** be in its
  timeslot's `TimeslotRoom` set (service-validated; both FKs are independently
  valid so a FK alone won't catch it).
- `UNIQUE(timeslot_id, room_id)` on `Placement` enforces "≤1 session per cell" at
  the DB layer, so admin drag/force-place can't double-book even outside the solver.
- **Candidate eligibility** for any solve filters `status='active'` **AND**
  `merged_into IS NULL`.

### Merge (non-destructive — the key fix)
Merging proposal B into A sets `B.merged_into = A`, `B.status='retired'`. **No
ratings or runners are ever mutated.** Folded ratings/runners are computed as a
*view* at solve/display time over the merge group: a participant's effective
stars for the group = `max(stars across the group)`; runners = union. This makes
merge **trivially reversible** (just clear `merged_into`), avoids duplicate-rating
bugs (the `Rating` unique constraint holds per real proposal), and removes the
whole "can't un-fold" class of error. Merge chains are resolved to a root and
**cycle-checked** (A→B→A rejected); merging an already-merged proposal re-points
within the group. After a merge the **runner set is re-validated** (a person now
running two sessions that could land in one slot is surfaced as a warning).

### Multi-slot / repeat (#14 — opt-in, cut-first)
`slots_needed>1` occupies **consecutive** timeslots in one room; represented by
**one `Placement` per occupied (timeslot, room) cell** sharing a `span` marker
(or a `placement_group_id`) so both the DB occupancy rule and the grid renderer
see every taken cell. `allow_repeat` lets the solver place a proposal in more
than one slot for coverage/capacity. Both default off. This is the highest-risk
feature and is isolated so it can be dropped without touching the core model.

## Engagement & presence model (load-bearing — defined explicitly)

These definitions drive the entire fairness objective; ambiguity here silently
changes results, so they are fixed:

- **Engaged participant (round-level):** someone who has rated ≥1 candidate
  proposal in this round at ≥ `INTERESTED_STARS` (=3), **or** is a runner of a
  candidate. Only engaged participants count toward coverage/fairness. People who
  never rated anything are **not** the organizer's problem to solve and are shown
  separately, never polluting the "left out" list.
- **Present (this round):** engaged **and** `RoundParticipant.present = true`.
  The organizer can flag someone absent (left the room / left the retreat); an
  absent runner has their runner-presence constraint dropped (with a "session X
  now has no runner" warning).
- **Unrated proposal pair:** `weight = 0`. Absence of a rating is never treated
  as implicit interest.

`present` is a **static parameter per round** (not per-slot, not a decision
variable) — interpretable as "engaged people who are here," keeping the fairness
term linear and reproducible.

## Solver (OR-Tools CP-SAT)

Small scale (dozens of people, ~10–20 candidates, 1–3 slots, 2–3 rooms/slot) ⇒
exact optimum in well under a second. **No heuristic fallback** — OR-Tools is a
controlled image dependency; a second approximate path would silently disagree
with CP-SAT and mislead the live preview. Cheapness for the preview comes from
server-side debounced caching (see Architecture), `max_time_in_seconds`, and a
fixed `random_seed` for stability.

**Interest weight (single documented table, one source of truth):**
`WEIGHT = {1:0, 2:1, 3:3, 4:6, 5:10}` (1–2 stars contribute ~nothing — "nothing
*good*"; runner's own session = effective 5 ⇒ 10). `INTERESTED_STARS = 3` is the
single threshold reused for eligibility, engagement, and the coverage gate.

**Eligibility (precompute):** a candidate proposal must have
`#{u : effective_stars(u,p) ≥ 3} ≥ round.min_interest`.

**Decision variables**
- `place[p,t,r] ∈ {0,1}` — proposal *p* runs in room *r* starting at slot *t*
  (occupies `t … t+slots_needed(p)-1`).
- `attend[u,p,τ] ∈ {0,1}` — participant *u* counted at *p* in slot *τ*.
  **Gated: created only for pairs with `effective_stars(u,p) ≥ 3`, plus all
  runner pairs (runners exempt from the gate).** This is the fix that makes
  coverage mean "has something they'd actually go to," not "could be shoved
  anywhere," and shrinks the model.
- `uncovered[u,t] ∈ {0,1}` — present participant *u* has nothing they'd attend in
  slot *t* (the fairness quantity — now actually defined and linked).
- `overflow[p,t] ∈ ℤ≥0` — projected attendance over the placed room's capacity.

**Constraints**
- **Occupancy / no double-book:** for each room *r* and slot *τ*,
  `Σ_{p,t : t ≤ τ ≤ t+slots_needed(p)-1} place[p,t,r] ≤ 1`. Disallow starts whose
  span runs past the last slot or into a slot where *r* isn't in `TimeslotRoom`.
- **Run count:** non-repeat `Σ_{t,r} place[p,t,r] ≤ 1`; repeat-allowed ≤ small cap.
- **Attend needs a running session:** for the gated vars,
  `attend[u,p,τ] ≤ Σ_r place[p, t(τ), r]` (for multi-slot, the start that covers τ).
- **One place per person per slot:** `Σ_p attend[u,p,τ] ≤ 1`.
- **Runner presence (modeled, not prose):** for each runner *u* of candidate *p*
  and each slot τ it occupies, `attend[u,p,τ] = Σ_r place[p,start,r]` — i.e. if u
  runs p, u is *at* p exactly when p runs. Combined with "one place per slot" this
  automatically (a) forbids a runner running/attending two things at once and
  (b) keeps them from being counted uncovered while running. Runner pairs are
  exempt from the interest gate (their attend var always exists).
- **Coverage linkage (the fix):** for each present *u* and slot *t*,
  `uncovered[u,t] ≥ 1 − Σ_p attend[u,p,t]`. Penalized in a maximize objective ⇒
  the `≥` floor is sufficient; the solver drives it to 0 when possible.
- **Capacity is soft, correct room index:**
  `overflow[p,t] ≥ Σ_u attend[u,p,t] − Σ_r capacity(r)·place[p,t,r]`, `overflow ≥ 0`.
  (When p isn't placed in t both terms vanish.) Overflow never causes
  infeasibility — it's allowed and surfaced as a flag.

**Objective (maximize)**
```
 Σ  WEIGHT(stars(u,p)) · attend[u,p,t]      # satisfied weighted interest
 − λ · Σ_{u present, t}  uncovered[u,t]      # fairness (admin slider)
 − κ · Σ_{p,t}  overflow[p,t]                # gentle capacity pressure
```
- **λ is interpretable:** "stars²-of-interest I'd forgo to cover one more person."
  Against the weight table (max 10), a sane slider is **0 … ~20**: λ=0 pure
  popularity; λ≈3 covers someone if it costs a lukewarm seat; λ≈10+ aggressively
  spreads. The slider shows this interpretation, not a raw number.
- **κ small and fixed** (≈1, below the smallest positive weight) so overflow is a
  tiebreaker/flag, not a driver — popular rooms *may* overflow (by design).

**Stability for the live preview:** fixed `random_seed`, single worker for the
preview solve, and a tiny epsilon bonus for retaining the previously-shown /
pinned placement, so the projected grid doesn't flicker between equal-objective
solutions as the last ratings land.

**Pinning safety:** pins are the only hard placements that can make the model
infeasible (two pins in one cell, pin violating availability/span). The service
**validates the pin set before solving** and, on conflict, reports "pin X can't
be honored" rather than returning a bare INFEASIBLE. Pinned placements are then
fixed `place=1` and the solver optimizes the rest around them.

**Result dataclass** asserts solver status (OPTIMAL/FEASIBLE) and carries: the
grid, per-slot coverage %, the named left-out set, capacity overflows, total
satisfied interest, and a flag distinguishing **chosen** uncovered (popularity
trade) from **physically forced** uncovered (present > total capacity in a slot —
"open another room").

> **Honesty note for the UI:** `attend`/`uncovered` are an optimizer assignment,
> not a forecast of real behavior. The UI says "under the optimizer's assignment,
> someone is uncovered," never "Alice *will* be left out."

## UI / UX

Design principle: **use all the pixels, but each surface earns its density.**
Phones get a single fast vertical task; the admin console is a dense command
center that can **collapse to "decision mode"** under time pressure. Mobile-first;
polling-tolerant. Clean, high-contrast, large tap targets. (Implementation uses
the frontend-design skill to avoid generic AI aesthetics.)

### Participant — mobile (one column, thumb-reachable)
- **Enter:** code + name; remembered via cookie. A persistent **state banner**
  always says where they are ("Round 2 — rating open" / "Round 2 — published, go
  to your session") so reopening the app after a locked phone lands them right.
- **Propose:** an always-visible title field + "＋ Propose," chat-message-like.
  Optional tiny details expander.
- **Rate (the spine):** scrollable list, each row = title + inline 5-star strip +
  a slightly **separated** "I'm running this" control (separated + easy-undo so a
  fat-finger doesn't silently add a hard presence constraint and lock stars to 5).
  Taps save optimistically (htmx). **Order is stable during a rating session**
  (unrated-first, frozen once you start); new proposals are **badged** ("2 new to
  rate") rather than reflowing the list under your thumb — and the nudge counts
  *new to rate*, never a fraction that regresses as N grows.
- **Published view:** compact per-timeslot grid (slot → room → session). Highlights
  *your top-rated session that's on at this time*. **Degrades for late joiners /
  non-raters:** with no ratings it shows the top sessions by aggregate interest
  per slot ("most popular here: Foo"), and rating now re-picks the highlight
  client-side over the frozen grid — no re-solve needed.
- **Notification/clock bar (sticky):** a wall-clock-anchored countdown computed
  from `started_at + duration` **on every load** (not a JS interval that dies when
  backgrounded), so reopening shows the *correct* remaining time. Best-effort Web
  Notification at slot start / "ends in 5:00" **only fires while foregrounded** —
  the bar is honest that the real signal is the organizer + the projected grid.

### Admin — desktop command center (dense; collapsible)
- **Left rail — controls & pool:** event/round selector; status; **λ fairness
  slider** (with its plain-language interpretation) and min-interest stepper, both
  re-previewing instantly; a **participation meter that splits rated / present /
  not-yet-rated**; proposal pool with retire/merge actions and per-proposal
  aggregates (avg, interested count, star histogram) — **aggregates suppressed
  below small-N (interested < 3)** so a 1–2 person rating can't be reverse-engineered.
- **Center — the grid:** timeslot × room matrix. Each cell card shows title,
  projected attendance vs capacity (fill bar, red on overflow), runner chips.
  **Every action has a non-drag fallback** (card menu: Move to… / Pin 📌 / Drop)
  so it works on a trackpad or tablet; drag is the fast path, not the only path.
  A "bench" strip holds eligible-but-unplaced candidates. Buttons: Generate /
  Re-solve (keep pins) / Publish (with a confirm summary: "N sessions, M left out").
- **Right rail — consequences (the verbal-decision fuel):** per-slot coverage %,
  the **named left-out list (admin screen only — warn against projecting)**,
  capacity overflows (split: chosen vs physically forced), total satisfied
  interest, and "top unplaced by interest." Crucially **relational**: hovering a
  left-out person highlights what they'd attend; hovering a candidate highlights
  *whom adding/merging it would rescue* — so "if I open one more room / merge
  these two, who does it actually help?" is answered on screen, not in the
  organizer's head. "On the bubble" sessions (near min-interest, flipping between
  recent solves) are marked vs "safely in," and "X people still rating" tells the
  organizer how settled the picture is before acting.
- **Decision mode:** a one-click collapse to just the grid + named left-out + the
  three buttons, for the panicky last 90 seconds; expand for full detail while calm.
- **Timeslot clocks:** per-slot Start sets `started_at`; the countdown then drives
  participant bars/notifications. **Unpublish** is allowed until the first clock
  starts (recover from a wrong publish).

### Archive
Reverse-chronological published rounds; each opens its frozen grid + stats as they
were at publish. Read-only; snapshots are display-only and **not reconstructable**
(documented, intentional).

### Empty/zero states
First round: "No proposals yet — propose the first one"; empty grid/bench/archive
all have designed empty states, not blank panels.

## Tech stack & architecture

- **Backend:** Python + **FastAPI** (uvicorn). **Frontend:** server-rendered
  **Jinja2** + **htmx** (polling + partial updates) + minimal vanilla JS only for
  the Web Notifications API and the wall-clock countdown. **No JS build step.**
- **DB:** **SQLite** (WAL) via **SQLAlchemy 2.0**. Event-scoping is centralized in
  the service layer (every query scoped by `event_id`) to prevent cross-event leaks.
- **Solver off the hot path (critical):** CP-SAT runs via `run_in_threadpool` /
  `asyncio.to_thread` so it never blocks request handling. The **live preview is a
  single server-side background loop**: it re-solves the latest "dirty" pool at
  most every N seconds and writes the grid+stats to a cache; admin polls just read
  the cache. So M admin polls + rating writes collapse to O(1) solves per interval
  regardless of poll count. Every solve carries `max_time_in_seconds` and returns
  best-feasible rather than hanging.
- **Module boundaries (single responsibility):**
  `app/` → `main.py` (wiring), `auth.py` (cookie signing/scope — unit-testable
  without HTTP), `db.py`, `models.py`,
  `routers/{participant,admin,events}.py`,
  `services/` (proposals, ratings, rounds, merge, presence),
  **`solver_input.py`** (DB rows → pure problem dataclass: eligibility, weight,
  presence, merge-folding — *its own tested unit*, because this mapping is where
  the real bugs live),
  `solver.py` (pure CP-SAT; no I/O; problem dataclass in, result dataclass out),
  `templates/`, `static/`.

## Persistence, deployment & backup

- **One Docker image** (python-slim, multi-stage). SQLite at `/data/app.db` on a
  mounted volume; `docker-compose.yml` for one-command run (service + named volume
  + `SECRET_KEY`, optional `BOOTSTRAP_ADMIN_CODE`).
- **Backup is v1, not optional:** a timer runs SQLite online backup
  (`VACUUM INTO` / `.backup`, WAL-safe) to a second path on a schedule, with a
  documented restore. The DB is the entire live state of an event happening *right
  now*; `RoundSnapshot` is archive presentation, **not** a backup.
- **Honest change/downtime framing:**
  - **Config changes** (events, rooms, rounds, proposals, λ): pure data → **no
    redeploy**.
  - **Code/template changes:** baked into the image → image rebuild + **brief
    (~1s) restart** (drops in-flight polls; htmx retries). This is *brief
    downtime*, not zero-downtime.
  - **Schema changes:** **Alembic migrations are a gated, explicit step — not
    auto-run on every boot.** A backup is taken immediately before migrating;
    SQLite table-rebuild migrations are slow/locky and are **not** "~1s." Migrations
    are tested against a populated copy.
  - True zero-downtime (blue-green behind a reverse proxy) is documented as an
    option but is **not** the single-container default.

## Security & privacy

- Two scopes in **signed** (`HttpOnly`/`Secure`/`SameSite=Lax`) cookies; admin
  scope lives **inside** the signed payload. Admin routers hard-check scope.
- **CSRF:** a token injected into htmx via `hx-headers` (meta tag → `htmx.config`),
  validated server-side on every state-changing POST; combined with SameSite.
- **Rate limiting:** the **admin code is the crown jewel** (a guesser controls the
  event and sees the named left-out list) → high-entropy admin code (not 4 digits)
  + per-IP and global exponential backoff/lockout on failed entry, with counters
  persisted (so a restart can't reset an attacker's backoff window).
- **`BOOTSTRAP_ADMIN_CODE`** is single-use / disabled after the first event is
  created — never a permanent env backdoor.
- **Ratings:** never returned per-person by any endpoint; aggregates suppressed
  below small-N. This is a UI boundary + social incentive, **not** a cryptographic
  guarantee (organizer has DB access) — stated plainly in Non-goals.

## Testing strategy (TDD)

- **`solver.py` unit tests (highest value), on fixtures, no DB/web:** uncovered is
  actually minimized by λ (λ=0 vs high flips popularity↔coverage); attend gate
  prevents junk coverage; runner never double-booked and never counted uncovered
  while running; absent runner drops the constraint; capacity overflow flagged not
  fatal and uses the right room; min-interest excludes runner-only sessions; pins
  honored and pin-conflict reported (not bare INFEASIBLE); multi-slot occupies
  consecutive cells; repeat only when it helps; chosen-vs-forced uncovered flag.
- **`solver_input.py` tests:** eligibility (`active AND merged_into IS NULL`),
  weight table, engaged/present derivation, merge-folding (max-star, runner union,
  chain resolution, cycle rejection).
- **Service tests:** propose/rate(upsert)/retire/merge(non-destructive +
  reversible)/present-toggle/participant-merge.
- **API + access-control tests:** participant cannot reach admin routes; no
  endpoint leaks per-person stars; small-N aggregate suppression; cookie identity
  persists ratings; CSRF rejection.
- **Smoke/e2e:** create event → propose → rate → preview → solve → pin → publish →
  start clock → archive → next round churn.

## Cut-safely list (descope order, last → first)
1. Repeat / multi-slot (#14) — opt-in flags, isolated in solver + placement rep.
2. Relational hover linking on the consequences rail (keep the lists; drop the join).
3. In-app proposal merge (#11) — verbal merge still works without it.
Core (engage/present model → propose → rate → solve with fairness+capacity+runners
→ pin → publish → archive → best-effort notify, with backup + honest ops) stands
on its own.
