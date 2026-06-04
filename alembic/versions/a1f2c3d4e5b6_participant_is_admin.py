"""participant is_admin

Revision ID: a1f2c3d4e5b6
Revises: 338ee87dc4dd
Create Date: 2026-06-03 15:30:00.000000

Adds Participant.is_admin so de-duplication of people by name is partitioned by
role: typing an organizer's name with a participant code can never grant admin,
and the same person re-logging in (or on a 2nd device) reuses one identity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f2c3d4e5b6'
down_revision: Union[str, Sequence[str], None] = '338ee87dc4dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('participants', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'is_admin', sa.Boolean(), nullable=False, server_default=sa.false()))
    # Backfill: the event creator is the earliest participant of each event.
    op.execute(
        "UPDATE participants SET is_admin = 1 WHERE id IN "
        "(SELECT MIN(id) FROM participants GROUP BY event_id)")


def downgrade() -> None:
    with op.batch_alter_table('participants', schema=None) as batch_op:
        batch_op.drop_column('is_admin')
