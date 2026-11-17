"""Document extractions: suggestions read automatically from uploaded files.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17 21:08:37.438437
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('document_extractions',
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('version_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=True),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('text_source', sa.String(length=32), nullable=True),
    sa.Column('fields', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('warnings', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reviewed_by_id', sa.Uuid(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('applied_fields', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'processing', 'extraction_completed', 'review_required', 'failed', 'verified', 'dismissed')", name=op.f('ck_document_extractions_status_valid')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_extractions_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_document_extractions_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['reviewed_by_id'], ['users.id'], name=op.f('fk_document_extractions_reviewed_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['version_id'], ['document_versions.id'], name=op.f('fk_document_extractions_version_id_document_versions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_extractions')),
    sa.UniqueConstraint('version_id', name=op.f('uq_document_extractions_version_id'))
    )
    with op.batch_alter_table('document_extractions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_extractions_document_id'), ['document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_document_extractions_organization_id'), ['organization_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('document_extractions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_extractions_organization_id'))
        batch_op.drop_index(batch_op.f('ix_document_extractions_document_id'))

    op.drop_table('document_extractions')
