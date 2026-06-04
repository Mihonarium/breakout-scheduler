from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request


def client_ip(request: Request) -> str:
    """Real client IP. Trust CF-Connecting-IP ONLY when the immediate peer is a
    trusted proxy (the Cloudflare tunnel connects from loopback) — otherwise the
    header is attacker-controlled and could be used to dodge rate limiting."""
    peer = request.client.host if request.client else None
    trusted = getattr(request.app.state.settings, "trusted_proxies", frozenset())
    if peer in trusted:
        return request.headers.get("cf-connecting-ip") or peer or "?"
    return peer or "?"


@dataclass
class _State:
    failures: int = 0
    blocked_until: float = 0.0


@dataclass
class Limiter:
    """Exponential backoff per key. Time is injected (`now` seconds) so the logic
    is deterministic and testable; callers pass a real monotonic clock."""
    base_s: float = 1.0
    cap_s: float = 60.0
    max_keys: int = 5000
    _state: dict[str, _State] = field(default_factory=dict)

    def allowed(self, key: str, now: float) -> bool:
        st = self._state.get(key)
        return st is None or now >= st.blocked_until

    def record_failure(self, key: str, now: float) -> None:
        if len(self._state) >= self.max_keys:  # evict expired entries to bound memory
            self._state = {k: v for k, v in self._state.items() if v.blocked_until > now}
        st = self._state.setdefault(key, _State())
        st.failures += 1
        delay = min(self.cap_s, self.base_s * (2 ** (st.failures - 1)))
        st.blocked_until = now + delay

    def reset(self, key: str) -> None:
        self._state.pop(key, None)
