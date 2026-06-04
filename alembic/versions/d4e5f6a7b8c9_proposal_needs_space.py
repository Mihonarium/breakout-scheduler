"""proposal needs_space + nullable placement room

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-03 17:30:00.000000

Adds Proposal.needs_space (False = an "elsewhere" session that needs no room) and
makes Placement.room_id nullable so a roomless session can be placed in a slot.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Names for the constraints Alembic must re-create when SQLite batch-rebuilds the
# table; without these the reflected (unnamed) UNIQUE/FK raise "Constraint must
# have a name" in batch mode.
_NAMING = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def upgrade() -> None:
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('needs_space', sa.Boolean(), nullable=False,
                                      server_default=sa.true()))
    with op.batch_alter_table('placements', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.alter_column('room_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('placements', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.alter_column('room_id', existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.drop_column('needs_space')
