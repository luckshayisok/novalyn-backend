"""Tenant scoping at the ORM layer.

Once a request has resolved its organization, the session carries the tenant ID
and every SELECT on a tenant-owned model is filtered by it. This is a second
line of defence behind the repositories, which also filter explicitly.
"""

import uuid

from sqlalchemy import ForeignKey, Uuid, event
from sqlalchemy.orm import Mapped, Session, declared_attr, mapped_column, with_loader_criteria

TENANT_INFO_KEY = "tenant_id"


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


def clear_tenant(session: Session) -> None:
    session.info.pop(TENANT_INFO_KEY, None)


def current_tenant(session: Session) -> uuid.UUID | None:
    return session.info.get(TENANT_INFO_KEY)


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
