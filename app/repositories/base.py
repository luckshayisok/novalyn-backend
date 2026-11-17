"""Base repository for tenant-owned data."""

import uuid
from typing import Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.tenancy import TenantScoped

ModelT = TypeVar("ModelT", bound=TenantScoped)


class TenantMismatchError(RuntimeError):
    """Raised when code tries to write a row into a different tenant than the repository's."""


class TenantRepository(Generic[ModelT]):
    """Every query made through this class is bound to one organization."""

    model: type[ModelT]

    def __init__(self, db: Session, organization_id: uuid.UUID) -> None:
        self.db = db
        self.organization_id = organization_id

    def _select(self) -> Select:
        return select(self.model).where(self.model.organization_id == self.organization_id)

    def _check(self, instance: ModelT) -> None:
        if instance.organization_id != self.organization_id:
            raise TenantMismatchError(
                f"{type(instance).__name__} does not belong to organization {self.organization_id}"
            )

    def get(self, instance_id: uuid.UUID) -> ModelT | None:
        return self.db.scalars(self._select().where(self.model.id == instance_id)).first()

    def add(self, instance: ModelT) -> ModelT:
        self._check(instance)
        self.db.add(instance)
        return instance

    def delete(self, instance: ModelT) -> None:
        self._check(instance)
        self.db.delete(instance)
