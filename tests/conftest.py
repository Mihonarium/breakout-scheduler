import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models import Base


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    # match production (app/db.py): enforce foreign keys so FK-violating writes
    # (e.g. deleting a participant still referenced by proposals) are caught in tests.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
