"""Tenant scoping, in two layers.

1. ORM filter: once a request has resolved its organization, every SELECT on a
   tenant-owned model is filtered by it (all databases).
2. PostgreSQL Row-Level Security: every transaction tells the database the
   current organization, the signed-in user, and whether this is a trusted
   system job (`app.tenant_id`, `app.user_id`, `app.rls_bypass`). The policies
   in `app/core/rls.py` then hide every other organization's rows, even from a
   query that forgot its filter.

The settings are transaction-local (`set_config(..., true)`): they end with
the transaction, so a pooled connection never carries them into another
request. They are re-applied at the start of every transaction from
`session.info`.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import ForeignKey, Uuid, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Session, declared_attr, mapped_column, with_loader_criteria

TENANT_INFO_KEY = "tenant_id"
USER_INFO_KEY = "rls_user_id"
BYPASS_INFO_KEY = "rls_bypass"


class TenantScoped:
    """Mixin for models owned by an organization."""

    @declared_attr
    def organization_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            Uuid,
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


def set_tenant(session: Session, organization_id: uuid.UUID) -> None:
    session.info[TENANT_INFO_KEY] = organization_id
    _sync(session)


def clear_tenant(session: Session) -> None:
    session.info.pop(TENANT_INFO_KEY, None)
    _sync(session)


def current_tenant(session: Session) -> uuid.UUID | None:
    return session.info.get(TENANT_INFO_KEY)


def set_rls_user(session: Session, user_id: uuid.UUID) -> None:
    """The signed-in user. Lets them see their own memberships in every organization."""
    session.info[USER_INFO_KEY] = user_id
    _sync(session)


def allow_all_tenants(session: Session) -> None:
    """Mark the session as a trusted system job (scheduler, worker, CLI, verified webhook).

    Row-Level Security then allows every organization. Never call this for a
    session that serves a person's request.
    """
    session.info[BYPASS_INFO_KEY] = True
    _sync(session)


@contextmanager
def all_tenants(session: Session) -> Iterator[None]:
    """Allow every organization for a few statements, for lookups by a credential
    (an invitation token) that name the organization only once found."""
    previous = session.info.get(BYPASS_INFO_KEY, False)
    allow_all_tenants(session)
    try:
        yield
    finally:
        session.info[BYPASS_INFO_KEY] = previous
        _sync(session)


def _values(info: dict) -> dict[str, str]:
    tenant = info.get(TENANT_INFO_KEY)
    user = info.get(USER_INFO_KEY)
    return {
        "tenant": str(tenant) if tenant else "",
        "user": str(user) if user else "",
        "bypass": "on" if info.get(BYPASS_INFO_KEY) else "off",
    }


def _apply(connection: Connection, info: dict) -> None:
    connection.execute(
        text(
            "SELECT set_config('app.tenant_id', :tenant, true),"
            " set_config('app.user_id', :user, true),"
            " set_config('app.rls_bypass', :bypass, true)"
        ),
        _values(info),
    )


def _sync(session: Session) -> None:
    """Apply a change at once when a transaction is already open; otherwise the next one picks it up."""
    if session.in_transaction() and session.get_bind().dialect.name == "postgresql":
        _apply(session.connection(), session.info)


@event.listens_for(Session, "after_begin")
def _start_transaction(session: Session, _transaction, connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        _apply(connection, session.info)


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(state) -> None:
    tenant_id = state.session.info.get(TENANT_INFO_KEY)
    if tenant_id is None or not state.is_select or state.is_column_load:
        return
    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda cls: cls.organization_id == tenant_id,
            include_aliases=True,
        )
    )
