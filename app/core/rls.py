"""PostgreSQL Row-Level Security policies for tenant-owned tables.

Applied by migration `0006` and, for databases built with `create_all` (the
test suite), by a metadata hook. SQLite has no row security; there the ORM
filter and the repositories are the isolation.

Every tenant-owned table gets one policy, `tenant_isolation`:

- rows are visible and writable only when `organization_id` equals the
  transaction's `app.tenant_id`, or the transaction is a trusted system job
  (`app.rls_bypass = 'on'`);
- `memberships` are also readable by the user they belong to
  (`app.user_id`), so a person can list their organizations before choosing
  one. Writing a membership still needs the organization.

`FORCE ROW LEVEL SECURITY` applies the policies to the table owner too. Only
superusers and roles with BYPASSRLS skip them, so the application must never
connect as one (checked at startup).
"""

from sqlalchemy import event, text
from sqlalchemy.engine import Connection

TENANT_TABLES = (
    "memberships",
    "invitations",
    "audit_logs",
    "entities",
    "documents",
    "document_versions",
    "document_extractions",
    "outbox_events",
    "notifications",
    "renewals",
    "renewal_events",
    "subscriptions",
    "invoices",
)

_BYPASS = "coalesce(current_setting('app.rls_bypass', true), '') = 'on'"
_TENANT = "organization_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
_USER = "user_id = nullif(current_setting('app.user_id', true), '')::uuid"


def policy_statements(table: str) -> list[str]:
    visible = f"{_BYPASS} OR {_TENANT}" + (f" OR {_USER}" if table == "memberships" else "")
    writable = f"{_BYPASS} OR {_TENANT}"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
        f"CREATE POLICY tenant_isolation ON {table} USING ({visible}) WITH CHECK ({writable})",
    ]


def removal_statements(table: str) -> list[str]:
    return [
        f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]


def enable_row_level_security(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for table in TENANT_TABLES:
        for statement in policy_statements(table):
            connection.execute(text(statement))


def disable_row_level_security(connection: Connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    for table in TENANT_TABLES:
        for statement in removal_statements(table):
            connection.execute(text(statement))


def role_bypasses_rls(connection: Connection) -> bool:
    """True when the connected role skips row security (superuser or BYPASSRLS)."""
    if connection.dialect.name != "postgresql":
        return False
    return bool(
        connection.execute(
            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).scalar()
    )


def register(metadata) -> None:
    """Create the policies whenever the schema is created with `create_all`."""

    @event.listens_for(metadata, "after_create")
    def _after_create(_target, connection: Connection, **_kw) -> None:
        enable_row_level_security(connection)
