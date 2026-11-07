"""jobs.params: job-kind parameters (the swept field and values of a sweep job)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07 12:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('params', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as b:
        b.drop_column('params')
