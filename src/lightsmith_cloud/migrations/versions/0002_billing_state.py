"""billing state: subscription period, event ordering, credit caps, overage metering

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01 12:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('orgs', sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orgs', sa.Column('cancel_at_period_end', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('orgs', sa.Column('billing_event_at', sa.BigInteger(), nullable=True))
    op.add_column('orgs', sa.Column('credit_limit', sa.BigInteger(), nullable=True))
    op.add_column('orgs', sa.Column('credit_cap_override', sa.BigInteger(), nullable=True))
    op.add_column('usage_records', sa.Column('overage_credits', sa.Float(), server_default='0', nullable=False))
    op.add_column('usage_records', sa.Column('billable_units', sa.Integer(), server_default='0', nullable=False))
    op.add_column('stripe_events', sa.Column('org_id', sa.String(length=200), nullable=True))
    op.add_column('stripe_events', sa.Column('outcome', sa.String(length=32), nullable=True))
    op.create_index(op.f('ix_stripe_events_org_id'), 'stripe_events', ['org_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_stripe_events_org_id'), table_name='stripe_events')
    with op.batch_alter_table('stripe_events') as b:
        b.drop_column('outcome')
        b.drop_column('org_id')
    with op.batch_alter_table('usage_records') as b:
        b.drop_column('billable_units')
        b.drop_column('overage_credits')
    with op.batch_alter_table('orgs') as b:
        b.drop_column('credit_cap_override')
        b.drop_column('credit_limit')
        b.drop_column('billing_event_at')
        b.drop_column('cancel_at_period_end')
        b.drop_column('current_period_end')
