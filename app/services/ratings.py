from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Rating


def set_rating(session: Session, *, participant_id: int, proposal_id: int,
               stars: int) -> Rating:
    if not 1 <= stars <= 5:
        raise ValueError("stars must be 1..5")
    existing = session.scalar(
        select(Rating).where(Rating.participant_id == participant_id,
                            Rating.proposal_id == proposal_id))
    if existing is None:
        existing = Rating(participant_id=participant_id, proposal_id=proposal_id,
                          stars=stars)
        session.add(existing)
    else:
        existing.stars = stars
    session.flush()
    return existing
