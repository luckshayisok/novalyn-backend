"""Billing: subscriptions, payment provider events and invoices.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17 21:59:38.862780
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('billing_events',
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('provider_event_id', sa.String(length=200), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_billing_events_organization_id_organizations'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_billing_events')),
    sa.UniqueConstraint('provider', 'provider_event_id', name=op.f('uq_billing_events_provider'))
    )
    with op.batch_alter_table('billing_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_billing_events_organization_id'), ['organization_id'], unique=False)

    op.create_table('invoices',
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('provider_invoice_id', sa.String(length=200), nullable=False),
    sa.Column('number', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=True),
    sa.Column('hosted_url', sa.String(length=1000), nullable=True),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_invoices_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invoices')),
    sa.UniqueConstraint('provider', 'provider_invoice_id', name=op.f('uq_invoices_provider'))
    )
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_invoices_organization_id'), ['organization_id'], unique=False)

    op.create_table('subscriptions',
    sa.Column('plan', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('billing_interval', sa.String(length=16), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=True),
    sa.Column('provider_customer_id', sa.String(length=200), nullable=True),
    sa.Column('provider_subscription_id', sa.String(length=200), nullable=True),
    sa.Column('pending_plan', sa.String(length=32), nullable=True),
    sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_at_period_end', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('custom_limits', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("plan IN ('free', 'starter', 'growth', 'business')", name=op.f('ck_subscriptions_plan_valid')),
    sa.CheckConstraint("status IN ('active', 'trialing', 'past_due', 'canceled', 'incomplete')", name=op.f('ck_subscriptions_status_valid')),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_subscriptions_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_subscriptions')),
    sa.UniqueConstraint('organization_id', name=op.f('uq_subscriptions_organization_id'))
    )
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_subscriptions_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_subscriptions_provider_subscription_id'), ['provider_subscription_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('subscriptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_subscriptions_provider_subscription_id'))
        batch_op.drop_index(batch_op.f('ix_subscriptions_organization_id'))

    op.drop_table('subscriptions')
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_invoices_organization_id'))

    op.drop_table('invoices')
    with op.batch_alter_table('billing_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_billing_events_organization_id'))

    op.drop_table('billing_events')
