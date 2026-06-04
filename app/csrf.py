from __future__ import annotations

from itsdangerous import URLSafeTimedSerializer, BadSignature

_SALT = "breakout-csrf-v1"
MAX_AGE_S = 8 * 3600


def new_token(secret: str) -> str:
    return URLSafeTimedSerializer(secret, salt=_SALT).dumps("csrf")


def verify(token: str | None, secret: str) -> bool:
    if not token:
        return False
    try:
        URLSafeTimedSerializer(secret, salt=_SALT).loads(token, max_age=MAX_AGE_S)
        return True
    except BadSignature:
        return False
    except Exception:
        return False
