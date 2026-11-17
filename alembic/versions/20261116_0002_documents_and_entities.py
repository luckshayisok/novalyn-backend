"""Documents, document versions and entities.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16 23:08:02.486563
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('entities',
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('code', sa.String(length=100), nullable=True),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('phone', sa.String(length=50), nullable=True),
    sa.Column('contact_name', sa.String(length=200), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('location_id', sa.Uuid(), nullable=True),
    sa.Column('department_id', sa.Uuid(), nullable=True),
    sa.Column('owner_membership_id', sa.Uuid(), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("type IN ('employee', 'supplier', 'equipment', 'location', 'department')", name=op.f('ck_entities_type_valid')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_entities_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['department_id'], ['entities.id'], name=op.f('fk_entities_department_id_entities'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['location_id'], ['entities.id'], name=op.f('fk_entities_location_id_entities'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_entities_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['owner_membership_id'], ['memberships.id'], name=op.f('fk_entities_owner_membership_id_memberships'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_entities'))
    )
    with op.batch_alter_table('entities', schema=None) as batch_op:
        batch_op.create_index('ix_entities_org_type', ['organization_id', 'type'], unique=False)
        batch_op.create_index(batch_op.f('ix_entities_organization_id'), ['organization_id'], unique=False)

    op.create_table('documents',
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=False),
    sa.Column('document_number', sa.String(length=100), nullable=True),
    sa.Column('issuing_authority', sa.String(length=200), nullable=True),
    sa.Column('holder_name', sa.String(length=200), nullable=True),
    sa.Column('issue_date', sa.Date(), nullable=True),
    sa.Column('expiry_date', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('entity_id', sa.Uuid(), nullable=True),
    sa.Column('owner_membership_id', sa.Uuid(), nullable=True),
    sa.Column('verification_status', sa.String(length=32), nullable=False),
    sa.Column('verified_by_id', sa.Uuid(), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rejection_reason', sa.String(length=500), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("verification_status IN ('pending', 'verified', 'rejected')", name=op.f('ck_documents_verification_status_valid')),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_documents_created_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['entity_id'], ['entities.id'], name=op.f('fk_documents_entity_id_entities'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_documents_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['owner_membership_id'], ['memberships.id'], name=op.f('fk_documents_owner_membership_id_memberships'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['verified_by_id'], ['users.id'], name=op.f('fk_documents_verified_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents'))
    )
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.create_index('ix_documents_entity_id', ['entity_id'], unique=False)
        batch_op.create_index('ix_documents_org_category', ['organization_id', 'category'], unique=False)
        batch_op.create_index('ix_documents_org_expiry', ['organization_id', 'expiry_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_organization_id'), ['organization_id'], unique=False)

    op.create_table('document_versions',
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('version_number', sa.Integer(), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=120), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('uploaded_by_id', sa.Uuid(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_versions_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_document_versions_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], name=op.f('fk_document_versions_uploaded_by_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_versions')),
    sa.UniqueConstraint('document_id', 'version_number', name=op.f('uq_document_versions_document_id'))
    )
    with op.batch_alter_table('document_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_versions_document_id'), ['document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_document_versions_organization_id'), ['organization_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('document_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_versions_organization_id'))
        batch_op.drop_index(batch_op.f('ix_document_versions_document_id'))

    op.drop_table('document_versions')
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_documents_organization_id'))
        batch_op.drop_index('ix_documents_org_expiry')
        batch_op.drop_index('ix_documents_org_category')
        batch_op.drop_index('ix_documents_entity_id')

    op.drop_table('documents')
    with op.batch_alter_table('entities', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_entities_organization_id'))
        batch_op.drop_index('ix_entities_org_type')

    op.drop_table('entities')
