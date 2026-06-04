from __future__ import annotations

from dataclasses import dataclass

from itsdangerous import URLSafeSerializer, BadSignature

_SALT = "breakout-session-v1"
SCOPES = ("participant", "admin")


@dataclass(frozen=True)
class SessionToken:
    pid: int
    scope: str          # "participant" | "admin"
    cb: int | None = None   # identity binding (participant created-at epoch); None = legacy cookie

    @property
    def is_admin(self) -> bool:
        return self.scope == "admin"


def issue_token(participant_id: int, scope: str, secret: str, cb: int | None = None) -> str:
    if scope not in SCOPES:
        raise ValueError(f"bad scope {scope!r}")
    data = {"pid": participant_id, "scope": scope}
    if cb is not None:
        data["cb"] = cb
    return URLSafeSerializer(secret, salt=_SALT).dumps(data)


def read_token(token: str | None, secret: str) -> SessionToken | None:
    if not token:
        return None
    try:
        data = URLSafeSerializer(secret, salt=_SALT).loads(token)
    except BadSignature:
        return None
    if not isinstance(data, dict) or data.get("scope") not in SCOPES:
        return None
    pid = data.get("pid")
    if not isinstance(pid, int):
        return None
    cb = data.get("cb")
    return SessionToken(pid=pid, scope=data["scope"], cb=cb if isinstance(cb, int) else None)
