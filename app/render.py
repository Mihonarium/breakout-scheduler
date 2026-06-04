from __future__ import annotations

import os

from fastapi import Request

from app.csrf import new_token
from app.deps import current_token

_STATIC = os.path.join(os.path.dirname(__file__), "static")


def _asset_v(name: str) -> int:
    """File mtime as a cache-busting version, so edited CSS/JS bypasses the CDN edge cache."""
    try:
        return int(os.path.getmtime(os.path.join(_STATIC, name)))
    except OSError:
        return 0


def render(request: Request, template: str, **ctx):
    templates = request.app.state.templates
    secret = request.app.state.settings.secret_key
    base = {
        "csrf_token": new_token(secret),
        "session": current_token(request),
        "css_v": _asset_v("styles.css"),
        "js_v": _asset_v("app.js"),
        "push_key": request.app.state.settings.vapid_public_key,
    }
    base.update(ctx)
    return templates.TemplateResponse(request, template, base)
