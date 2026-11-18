"""Entities: the employees, suppliers, equipment, locations and departments that documents belong to."""

# Methods named `list` shadow the builtin inside these classes; postponed
# evaluation keeps `list[...]` in later annotations meaning the builtin.
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.constants import ENTITY_TYPES
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.plans import Feature
from app.core.time import utcnow
from app.models import Entity, Membership
from app.repositories.documents import EntityDocumentCounts, EntityRepository
from app.repositories.organizations import MembershipRepository
from app.services.audit import AuditAction, AuditService
from app.services.context import OrgContext
from app.services.expiry import organization_today
from app.services.plans import Entitlements

_LINK_TYPES = {"location_id": "location", "department_id": "department"}
_TEXT_FIELDS = ("name", "code", "email", "phone", "contact_name", "address", "notes")


def field_error(field: str, message: str) -> ValidationFailedError:
    return ValidationFailedError(message, details=[{"field": field, "message": message}])


def resolve_owner(db: Session, ctx: OrgContext, membership_id: uuid.UUID | None) -> Membership | None:
    """The membership an item is assigned to; it must belong to this organization."""
    if membership_id is None:
        return None
    membership = MembershipRepository(db, ctx.organization_id).get(membership_id)
    if membership is None:
        raise field_error("owner_membership_id", "Choose a member of this organization.")
    return membership


class EntityService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    def list(
        self, ctx: OrgContext, *, type_: str | None, q: str | None, archived: bool, page: int, page_size: int
    ) -> tuple[list[Entity], int, dict[uuid.UUID, EntityDocumentCounts]]:
        entities = EntityRepository(self.db, ctx.organization_id)
        items, total = entities.list(type_=type_, q=q, archived=archived, page=page, page_size=page_size)
        counts = entities.document_counts([item.id for item in items], self._today(ctx))
        return items, total, counts

    def lookup(self, ctx: OrgContext, *, type_: str | None, q: str | None, limit: int = 50) -> list[Entity]:
        return EntityRepository(self.db, ctx.organization_id).lookup(type_=type_, q=q, limit=limit)

    def count_by_type(self, ctx: OrgContext) -> dict[str, int]:
        return EntityRepository(self.db, ctx.organization_id).count_by_type()

    def get(self, ctx: OrgContext, entity_id: uuid.UUID) -> tuple[Entity, EntityDocumentCounts]:
        entities = EntityRepository(self.db, ctx.organization_id)
        entity = entities.get(entity_id)
        if entity is None:
            raise NotFoundError("This record was not found.")
        counts = entities.document_counts([entity.id], self._today(ctx)).get(
            entity.id, EntityDocumentCounts()
        )
        return entity, counts

    def create(self, ctx: OrgContext, data: dict[str, Any]) -> Entity:
        entitlements = Entitlements(self.db, ctx.organization_id)
        entitlements.require(Feature.ENTITY_MANAGEMENT)
        if data["type"] == "location":
            entitlements.check_locations()
        entity = Entity(organization_id=ctx.organization_id, type=data["type"], created_by_id=ctx.user.id)
        self._apply(ctx, entity, {key: value for key, value in data.items() if key != "type"})
        EntityRepository(self.db, ctx.organization_id).add(entity)
        self.db.flush()
        self.audit.record_for(
            ctx,
            action=AuditAction.ENTITY_CREATED,
            resource_type="entity",
            resource_id=entity.id,
            metadata={"name": entity.name, "type": entity.type},
        )
        self.db.commit()
        return entity

    def update(self, ctx: OrgContext, entity_id: uuid.UUID, changes: dict[str, Any]) -> Entity:
        entity, _ = self.get(ctx, entity_id)
        changed = self._apply(ctx, entity, changes)
        if changed:
            self.audit.record_for(
                ctx,
                action=AuditAction.ENTITY_UPDATED,
                resource_type="entity",
                resource_id=entity.id,
                metadata={"name": entity.name, "type": entity.type, "fields": changed},
            )
            self.db.commit()
        return entity

    def set_archived(self, ctx: OrgContext, entity_id: uuid.UUID, archived: bool) -> Entity:
        entity, _ = self.get(ctx, entity_id)
        if (entity.archived_at is not None) == archived:
            return entity
        if not archived:
            entitlements = Entitlements(self.db, ctx.organization_id)
            entitlements.require(Feature.ENTITY_MANAGEMENT)
            if entity.type == "location":
                entitlements.check_locations()
        entity.archived_at = utcnow() if archived else None
        self.audit.record_for(
            ctx,
            action=AuditAction.ENTITY_ARCHIVED if archived else AuditAction.ENTITY_RESTORED,
            resource_type="entity",
            resource_id=entity.id,
            metadata={"name": entity.name, "type": entity.type},
        )
        self.db.commit()
        return entity

    # ------------------------------------------------------------------ helpers

    def _today(self, ctx: OrgContext):
        return organization_today(ctx.organization.timezone)

    def _apply(self, ctx: OrgContext, entity: Entity, changes: dict[str, Any]) -> list[str]:
        """Validate and set fields. Returns the names of the fields that changed."""
        changed: list[str] = []
        entities = EntityRepository(self.db, ctx.organization_id)

        for field in _TEXT_FIELDS:
            if field not in changes:
                continue
            value = changes[field]
            value = value.strip() or None if isinstance(value, str) else None
            if field == "name" and not value:
                raise field_error("name", "Enter a name.")
            if getattr(entity, field) != value:
                setattr(entity, field, value)
                changed.append(field)

        for field, required_type in _LINK_TYPES.items():
            if field not in changes:
                continue
            target_id = changes[field]
            target: Entity | None = None
            if target_id is not None:
                label = ENTITY_TYPES[required_type].lower().rstrip("s")
                if entity.type == required_type:
                    raise field_error(field, f"A {label} cannot be placed inside another {label}.")
                target = entities.get(target_id)
                if target is None or target.type != required_type or target.archived_at is not None:
                    raise field_error(field, f"Choose an existing {label}.")
            if getattr(entity, field) != target_id:
                # Set through the relationship so the loaded object and the foreign key stay in step.
                setattr(entity, required_type, target)
                changed.append(field)

        if "owner_membership_id" in changes:
            owner = resolve_owner(self.db, ctx, changes["owner_membership_id"])
            owner_id = owner.id if owner else None
            if entity.owner_membership_id != owner_id:
                entity.owner = owner
                changed.append("owner_membership_id")

        return changed
