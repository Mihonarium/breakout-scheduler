import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models import Base, Event, Participant, Proposal, Rating


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_rating_basic_relations():
    with _session() as s:
        e = Event(name="Retreat", participant_code="p", admin_code="a")
        s.add(e); s.flush()
        u = Participant(event_id=e.id, display_name="Sam"); s.add(u); s.flush()
        pr = Proposal(event_id=e.id, title="AI safety", created_by=u.id)
        s.add(pr); s.flush()
        s.add(Rating(participant_id=u.id, proposal_id=pr.id, stars=5))
        s.commit()
        got = s.scalar(select(Rating).where(Rating.participant_id == u.id))
        assert got.stars == 5


def test_rating_unique_per_participant_proposal():
    with _session() as s:
        e = Event(name="R", participant_code="p", admin_code="a")
        s.add(e); s.flush()
        u = Participant(event_id=e.id, display_name="Sam"); s.add(u); s.flush()
        pr = Proposal(event_id=e.id, title="T", created_by=u.id); s.add(pr); s.flush()
        s.add(Rating(participant_id=u.id, proposal_id=pr.id, stars=3)); s.commit()
        s.add(Rating(participant_id=u.id, proposal_id=pr.id, stars=5))
        with pytest.raises(IntegrityError):
            s.commit()
