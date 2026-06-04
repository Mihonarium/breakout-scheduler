from __future__ import annotations

from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session

from app.deps import get_session, require_participant, require_csrf
from app.services import push as PUSH

router = APIRouter()


@router.post("/push/subscribe")
async def subscribe(request: Request, token=Depends(require_participant),
                    session: Session = Depends(get_session),
                    _: None = Depends(require_csrf)):
    if not request.app.state.settings.push_enabled:
        raise HTTPException(status_code=503, detail="push not configured")
    try:
        sub = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid body")
    PUSH.subscribe(session, participant_id=token.pid, sub=sub)
    return {"ok": True}
