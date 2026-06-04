from __future__ import annotations

from typing import Callable

from app.types import Result


class PreviewCache:
    """Server-side debounced cache of preview solves. A single background loop
    calls `refresh_due`; many polling clients just `get`. Time is injected so the
    debounce is deterministic and testable."""

    def __init__(self, interval_s: float = 3.0):
        self.interval_s = interval_s
        self._results: dict[int, Result] = {}
        self._prev: dict[int, Result] = {}     # one-back, for "on the bubble" diffing
        self._dirty: dict[int, bool] = {}
        self._last_solved: dict[int, float] = {}

    def mark_dirty(self, round_id: int) -> None:
        self._dirty[round_id] = True

    def get(self, round_id: int) -> Result | None:
        return self._results.get(round_id)

    def previous(self, round_id: int) -> Result | None:
        return self._prev.get(round_id)

    def refresh_due(self, now: float, solve_fn: Callable[[int], Result]) -> list[int]:
        refreshed = []
        for round_id, dirty in list(self._dirty.items()):
            if not dirty:
                continue
            if now - self._last_solved.get(round_id, -1e9) < self.interval_s:
                continue
            if round_id in self._results:
                self._prev[round_id] = self._results[round_id]
            self._results[round_id] = solve_fn(round_id)
            self._dirty[round_id] = False
            self._last_solved[round_id] = now
            refreshed.append(round_id)
        return refreshed
