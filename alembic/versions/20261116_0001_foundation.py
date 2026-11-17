"""Foundation: users, sessions, organizations, memberships, invitations, audit log.

Revision ID: 0001
Revises: -
Create Date: 2026-11-16 22:26:47.751741
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('full_name', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('email_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('organizations',
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=80), nullable=False),
    sa.Column('industry', sa.String(length=80), nullable=True),
    sa.Column('company_size', sa.String(length=32), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('document_categories', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('onboarding_completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_organizations_created_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations')),
    sa.UniqueConstraint('slug', name=op.f('uq_organizations_slug'))
    )
    op.create_table('refresh_tokens',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('family_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_agent', sa.String(length=400), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_refresh_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refresh_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_refresh_tokens_token_hash'))
    )
    with op.batch_alter_table('refresh_tokens', schema=None) as batch_op:
        batch_op.create_index('ix_refresh_tokens_family_id', ['family_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_refresh_tokens_user_id'), ['user_id'], unique=False)

    op.create_table('user_tokens',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_tokens_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_user_tokens_token_hash'))
    )
    with op.batch_alter_table('user_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_tokens_user_id'), ['user_id'], unique=False)

    op.create_table('audit_logs',
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('resource_type', sa.String(length=50), nullable=False),
    sa.Column('resource_id', sa.String(length=64), nullable=True),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_logs_actor_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_audit_logs_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.create_index('ix_audit_logs_org_created', ['organization_id', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_organization_id'), ['organization_id'], unique=False)

    op.create_table('invitations',
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('invited_by_id', sa.Uuid(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('accepted_by_id', sa.Uuid(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("role IN ('owner', 'admin', 'compliance_manager', 'member', 'viewer')", name=op.f('ck_invitations_role_valid')),
    sa.ForeignKeyConstraint(['accepted_by_id'], ['users.id'], name=op.f('fk_invitations_accepted_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['invited_by_id'], ['users.id'], name=op.f('fk_invitations_invited_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_invitations_organization_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invitations')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_invitations_token_hash'))
    )
    with op.batch_alter_table('invitations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_invitations_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index('uq_invitations_open_email', ['organization_id', 'email'], unique=True, postgresql_where=sa.text('accepted_at IS NULL AND revoked_at IS NULL'), sqlite_where=sa.text('accepted_at IS NULL AND revoked_at IS NULL'))

    op.create_table('memberships',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("role IN ('owner', 'admin', 'compliance_manager', 'member', 'viewer')", name=op.f('ck_memberships_role_valid')),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_memberships_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_memberships_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memberships')),
    sa.UniqueConstraint('organization_id', 'user_id', name=op.f('uq_memberships_organization_id'))
    )
    with op.batch_alter_table('memberships', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_memberships_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_memberships_user_id'), ['user_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('memberships', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_memberships_user_id'))
        batch_op.drop_index(batch_op.f('ix_memberships_organization_id'))

    op.drop_table('memberships')
    with op.batch_alter_table('invitations', schema=None) as batch_op:
        batch_op.drop_index('uq_invitations_open_email', postgresql_where=sa.text('accepted_at IS NULL AND revoked_at IS NULL'), sqlite_where=sa.text('accepted_at IS NULL AND revoked_at IS NULL'))
        batch_op.drop_index(batch_op.f('ix_invitations_organization_id'))

    op.drop_table('invitations')
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_logs_organization_id'))
        batch_op.drop_index('ix_audit_logs_org_created')

    op.drop_table('audit_logs')
    with op.batch_alter_table('user_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_tokens_user_id'))

    op.drop_table('user_tokens')
    with op.batch_alter_table('refresh_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_refresh_tokens_user_id'))
        batch_op.drop_index('ix_refresh_tokens_family_id')

    op.drop_table('refresh_tokens')
    op.drop_table('organizations')
    op.drop_table('users')
