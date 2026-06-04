"""Exercise the Alembic upgrade path that production uses with RUN_MIGRATIONS=1.
The test suite otherwise builds schema via create_all, so this is the only thing
guarding against migration breakage (e.g. unnamed constraints in SQLite batch mode)."""
import os
import tempfile

from sqlalchemy import create_engine, inspect
from alembic import command
from alembic.config import Config as AlembicConfig

from app.models import Base

ROOT = os.path.dirname(os.path.dirname(__file__))


def _alembic_cfg(db_path: str) -> AlembicConfig:
    cfg = AlembicConfig(os.path.join(ROOT, "alembic.ini"))
    os.environ["DB_PATH"] = db_path
    return cfg


def test_alembic_upgrade_head_on_fresh_db():
    """`alembic upgrade head` must apply cleanly start-to-finish on an empty DB."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "fresh.db")
        command.upgrade(_alembic_cfg(db), "head")   # must not raise
        cols = {c["name"] for c in inspect(create_engine(f"sqlite:///{db}"))
                .get_columns("proposals")}
        # columns added across the migration chain are all present
        assert {"round_id", "min_interest_override", "max_attendees",
                "anonymous", "must_run"} <= cols


def test_migrated_schema_matches_create_all():
    """The two provisioning paths (Alembic vs create_all) must converge, so the
    fast-restart create_all path can't silently drift from migrated deploys."""
    with tempfile.TemporaryDirectory() as d:
        migrated = os.path.join(d, "migrated.db")
        command.upgrade(_alembic_cfg(migrated), "head")
        fresh = create_engine("sqlite://")
        Base.metadata.create_all(fresh)

        mig = inspect(create_engine(f"sqlite:///{migrated}"))
        new = inspect(fresh)
        for table in ("proposals", "participants", "timeslot_rooms"):
            assert ({c["name"] for c in mig.get_columns(table)}
                    == {c["name"] for c in new.get_columns(table)}), table
