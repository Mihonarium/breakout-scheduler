"""proposal anonymous

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-03 16:30:00.000000

Lets a participant propose a session without their name attached. An anonymous
proposal is not auto-assigned its proposer as a runner (that would reveal them);
the proposer keeps edit rights via created_by, which is never displayed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'anonymous', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('proposals', schema=None) as batch_op:
        batch_op.drop_column('anonymous')
