"""PostgreSQL Row-Level Security: the database itself hides other organizations' rows.

These tests use raw SQL, which skips the ORM tenant filter, so only the
database policies stand between one organization and another. They run when
the suite runs on PostgreSQL (TEST_DATABASE_URL) and are skipped on SQLite.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.database import Base, SessionLocal, engine
from app.core.rls import TENANT_TABLES, role_bypasses_rls
from app.core.tenancy import all_tenants, set_rls_user, set_tenant
from app.models import Entity

# Tables with an organization column that are deliberately not tenant-owned.
NOT_TENANT_OWNED = {"billing_events"}  # provider events, some with no organization; read only by system jobs

postgres_only = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="Row-Level Security needs PostgreSQL"
)


def test_every_tenant_table_has_a_policy():
    """A new table with organization_id must be added to TENANT_TABLES (or exempted here, with a reason)."""
    with_org = {name for name, table in Base.metadata.tables.items() if "organization_id" in table.columns}
    assert with_org - NOT_TENANT_OWNED == set(TENANT_TABLES)


@pytest.fixture
def two_orgs(actor):
    alice = actor("alice@example.com", "Alice")
    bob = actor("bob@example.com", "Bob")
    org_a = alice.create_org("Alpha")["id"]
    org_b = bob.create_org("Beta")["id"]
    alice.uploaded(org_a, name="Alpha permit")
    bob.uploaded(org_b, name="Beta permit")
    bob.uploaded(org_b, name="Beta licence")
    return {"a": uuid.UUID(org_a), "b": uuid.UUID(org_b), "alice": alice, "bob": bob}


def _orgs(db, table: str) -> list[uuid.UUID]:
    return list(db.execute(text(f"SELECT organization_id FROM {table}")).scalars())  # noqa: S608 - fixed names


@postgres_only
def test_policies_are_enabled_and_forced(two_orgs):
    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
                " exists(SELECT 1 FROM pg_policies p WHERE p.tablename = c.relname"
                "        AND p.policyname = 'tenant_isolation')"
                " FROM pg_class c WHERE c.relname = ANY(:tables) AND c.relkind = 'r'"
            ),
            {"tables": list(TENANT_TABLES)},
        ).all()
        assert {row[0] for row in rows} == set(TENANT_TABLES)
        assert all(row[1] and row[2] and row[3] for row in rows), rows
        assert role_bypasses_rls(db.connection()) is False  # the suite connects as an ordinary role


@postgres_only
def test_no_organization_means_no_rows(two_orgs):
    with SessionLocal() as db:
        for table in ("documents", "document_versions", "audit_logs", "subscriptions", "memberships"):
            assert _orgs(db, table) == [], table


@postgres_only
def test_each_organization_sees_only_its_own_rows(two_orgs):
    with SessionLocal() as db:
        set_tenant(db, two_orgs["a"])
        assert _orgs(db, "documents") == [two_orgs["a"]]
        assert set(_orgs(db, "audit_logs")) == {two_orgs["a"]}
        db.commit()  # the setting is re-applied to the next transaction
        assert db.execute(text("SELECT name FROM documents")).scalars().all() == ["Alpha permit"]

        set_tenant(db, two_orgs["b"])
        assert sorted(db.execute(text("SELECT name FROM documents")).scalars()) == [
            "Beta licence",
            "Beta permit",
        ]


@postgres_only
def test_writing_into_another_organization_is_refused(two_orgs):
    with SessionLocal() as db:
        set_tenant(db, two_orgs["a"])
        db.add(Entity(organization_id=two_orgs["b"], type="supplier", name="Planted"))
        with pytest.raises(DBAPIError, match="row-level security"):
            db.flush()
        db.rollback()

        # Updating or deleting another organization's rows touches nothing: they are invisible.
        set_tenant(db, two_orgs["a"])
        changed = db.execute(text("UPDATE documents SET name = 'Hijacked' WHERE organization_id = :b"),
                             {"b": two_orgs["b"]}).rowcount  # fmt: skip
        removed = db.execute(
            text("DELETE FROM audit_logs WHERE organization_id = :b"), {"b": two_orgs["b"]}
        ).rowcount
        assert (changed, removed) == (0, 0)
        db.rollback()


@postgres_only
def test_a_person_sees_their_own_memberships_everywhere_but_no_one_elses(two_orgs):
    alice_id = uuid.UUID(two_orgs["alice"].user["id"])
    with SessionLocal() as session:
        set_rls_user(session, alice_id)
        rows = session.execute(text("SELECT organization_id, user_id FROM memberships")).all()
        assert rows == [(two_orgs["a"], alice_id)]


@postgres_only
def test_settings_do_not_leak_between_transactions_on_a_pooled_connection(two_orgs):
    with SessionLocal() as db:
        set_tenant(db, two_orgs["a"])
        assert _orgs(db, "documents") == [two_orgs["a"]]
        db.commit()
    # The connection goes back to the pool; a fresh session on it must see nothing.
    with SessionLocal() as db:
        assert _orgs(db, "documents") == []
        assert db.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar() in (None, "")


@postgres_only
def test_system_scope_is_explicit_and_temporary(two_orgs):
    with SessionLocal() as db:
        with all_tenants(db):
            assert set(_orgs(db, "documents")) == {two_orgs["a"], two_orgs["b"]}
        assert _orgs(db, "documents") == []
