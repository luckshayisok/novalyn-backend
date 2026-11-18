"""PostgreSQL Row-Level Security on every tenant-owned table.

Does nothing on SQLite. The policies are defined in `app/core/rls.py`, the
same definitions the test suite applies after `create_all`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18 14:00:00
"""

from collections.abc import Sequence

from alembic import op

from app.core.rls import disable_row_level_security, enable_row_level_security

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    enable_row_level_security(op.get_bind())


def downgrade() -> None:
    disable_row_level_security(op.get_bind())
