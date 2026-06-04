"""proposal max_attendees

Revision ID: b2c3d4e5f6a7
Revises: a1f2c3d4e5b6
Create Date: 2026-06-03 15:45:00.000000

Per-session host seat cap. The solver never *seats* (counts as attending) more
people than this — effective cap = min(room seats, this) — and routes the
overflow to their next-best session. A popular session is not penalised for
being capped; it just right-sizes the room.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1f2c3d4e5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_attendees', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.drop_column('max_attendees')
