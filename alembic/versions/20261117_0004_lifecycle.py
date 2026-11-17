"""Lifecycle: notification outbox, notifications, renewals, reminder settings, version state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17 21:29:47.999637
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('outbox_events',
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=500), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'processing', 'done', 'failed')", name=op.f('ck_outbox_events_status_valid')),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_outbox_events_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbox_events')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_outbox_events_dedupe_key'))
    )
    with op.batch_alter_table('outbox_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_outbox_events_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index('ix_outbox_events_status_next', ['status', 'next_attempt_at'], unique=False)

    op.create_table('notifications',
    sa.Column('event_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.String(length=1000), nullable=False),
    sa.Column('link', sa.String(length=300), nullable=True),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('email_status', sa.String(length=16), nullable=False),
    sa.Column('email_attempts', sa.Integer(), nullable=False),
    sa.Column('email_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['event_id'], ['outbox_events.id'], name=op.f('fk_notifications_event_id_outbox_events'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_notifications_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_notifications_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    sa.UniqueConstraint('event_id', 'user_id', name=op.f('uq_notifications_event_id'))
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index('ix_notifications_user_org_created', ['user_id', 'organization_id', 'created_at'], unique=False)

    op.create_table('renewals',
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('assignee_membership_id', sa.Uuid(), nullable=True),
    sa.Column('deadline', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('previous_version_id', sa.Uuid(), nullable=True),
    sa.Column('new_version_id', sa.Uuid(), nullable=True),
    sa.Column('proposed_issue_date', sa.Date(), nullable=True),
    sa.Column('proposed_expiry_date', sa.Date(), nullable=True),
    sa.Column('previous_expiry_date', sa.Date(), nullable=True),
    sa.Column('rejection_reason', sa.String(length=500), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("status IN ('not_started', 'requested', 'in_progress', 'submitted', 'awaiting_approval', 'completed', 'rejected')", name=op.f('ck_renewals_status_valid')),
    sa.ForeignKeyConstraint(['assignee_membership_id'], ['memberships.id'], name=op.f('fk_renewals_assignee_membership_id_memberships'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['completed_by_id'], ['users.id'], name=op.f('fk_renewals_completed_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_renewals_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_renewals_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['new_version_id'], ['document_versions.id'], name=op.f('fk_renewals_new_version_id_document_versions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_renewals_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['previous_version_id'], ['document_versions.id'], name=op.f('fk_renewals_previous_version_id_document_versions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_renewals'))
    )
    with op.batch_alter_table('renewals', schema=None) as batch_op:
        batch_op.create_index('ix_renewals_org_status', ['organization_id', 'status'], unique=False)
        batch_op.create_index(batch_op.f('ix_renewals_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index('uq_renewals_open_document', ['document_id'], unique=True, postgresql_where=sa.text("status NOT IN ('completed', 'rejected')"), sqlite_where=sa.text("status NOT IN ('completed', 'rejected')"))

    op.create_table('renewal_events',
    sa.Column('renewal_id', sa.Uuid(), nullable=False),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('from_status', sa.String(length=32), nullable=True),
    sa.Column('to_status', sa.String(length=32), nullable=False),
    sa.Column('note', sa.String(length=1000), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_renewal_events_actor_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_renewal_events_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['renewal_id'], ['renewals.id'], name=op.f('fk_renewal_events_renewal_id_renewals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_renewal_events'))
    )
    with op.batch_alter_table('renewal_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_renewal_events_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_renewal_events_renewal_id'), ['renewal_id'], unique=False)

    with op.batch_alter_table('document_versions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('state', sa.String(length=16), server_default=sa.text("'active'"), nullable=False))
        batch_op.create_check_constraint("state_valid", "state IN ('active', 'pending', 'rejected')")

    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reminders_enabled', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch_op.add_column(sa.Column('reminder_offsets', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), server_default=sa.text("'[60, 30, 7, 1, 0]'"), nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.drop_column('reminder_offsets')
        batch_op.drop_column('reminders_enabled')

    with op.batch_alter_table('document_versions', schema=None) as batch_op:
        batch_op.drop_constraint(op.f('ck_document_versions_state_valid'), type_='check')
        batch_op.drop_column('state')

    with op.batch_alter_table('renewal_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_renewal_events_renewal_id'))
        batch_op.drop_index(batch_op.f('ix_renewal_events_organization_id'))

    op.drop_table('renewal_events')
    with op.batch_alter_table('renewals', schema=None) as batch_op:
        batch_op.drop_index('uq_renewals_open_document', postgresql_where=sa.text("status NOT IN ('completed', 'rejected')"), sqlite_where=sa.text("status NOT IN ('completed', 'rejected')"))
        batch_op.drop_index(batch_op.f('ix_renewals_organization_id'))
        batch_op.drop_index('ix_renewals_org_status')

    op.drop_table('renewals')
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_index('ix_notifications_user_org_created')
        batch_op.drop_index(batch_op.f('ix_notifications_organization_id'))

    op.drop_table('notifications')
    with op.batch_alter_table('outbox_events', schema=None) as batch_op:
        batch_op.drop_index('ix_outbox_events_status_next')
        batch_op.drop_index(batch_op.f('ix_outbox_events_organization_id'))

    op.drop_table('outbox_events')
