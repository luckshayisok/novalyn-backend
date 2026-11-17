"""Dashboard figures and global search. Everything is computed from the organization's own records."""

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, Entity
from app.repositories.documents import DocumentRepository, EntityRepository, _contains
from app.services.context import OrgContext
from app.services.expiry import expiring_window_days, organization_today, organization_zone

UPCOMING_MONTHS = 6
LIST_SIZE = 8
GROUP_LIMIT = 8


@dataclass(frozen=True)
class GroupCount:
    entity_id: str
    name: str
    type: str
    count: int


@dataclass(frozen=True)
class Dashboard:
    today: date
    expiring_window_days: int
    metrics: dict[str, int]
    by_category: list[tuple[str, int]]
    upcoming_expirations: list[tuple[str, int]]
    by_group: list[GroupCount]
    ungrouped_count: int
    expiring_documents: list[Document]
    expired_documents: list[Document]
    recent_documents: list[Document]
    open_renewals: list = field(default_factory=list)
    renewal_trend: list[tuple[str, int, int]] = field(default_factory=list)


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


class DashboardService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def build(self, ctx: OrgContext) -> Dashboard:
        today = organization_today(ctx.organization.timezone)
        documents = DocumentRepository(self.db, ctx.organization_id)

        metrics = documents.status_counts(today)
        metrics["archived"] = documents.archived_count()
        from app.services.extraction import ExtractionRepository

        metrics["suggestions_to_review"] = ExtractionRepository(self.db, ctx.organization_id).count_ready()

        from app.models import Renewal, RenewalStatus
        from app.services.renewals import RenewalRepository

        renewals = RenewalRepository(self.db, ctx.organization_id)
        counts = renewals.counts(today)
        metrics["renewals_in_progress"] = counts["open"]
        metrics["renewals_overdue"] = counts["overdue"]
        metrics["renewals_awaiting_approval"] = counts["awaiting_approval"]
        open_renewals, _ = renewals.list(
            statuses=RenewalStatus.OPEN, assignee_id=None, page=1, page_size=LIST_SIZE
        )

        # Renewals started and completed in each of the last six months, this month included.
        first_month = today.replace(day=1)
        trend_start = _add_months(first_month, -(UPCOMING_MONTHS - 1))
        trend = {_month_key(_add_months(trend_start, offset)): [0, 0] for offset in range(UPCOMING_MONTHS)}
        rows = self.db.execute(
            select(Renewal.created_at, Renewal.completed_at, Renewal.status).where(
                Renewal.organization_id == ctx.organization_id
            )
        ).all()
        zone = organization_zone(ctx.organization.timezone)
        for created_at, completed_at, status in rows:
            started = _month_key(created_at.astimezone(zone).date())
            if started in trend:
                trend[started][0] += 1
            if status == RenewalStatus.COMPLETED and completed_at is not None:
                finished = _month_key(completed_at.astimezone(zone).date())
                if finished in trend:
                    trend[finished][1] += 1

        # Expirations per calendar month, from today to the end of the sixth month.
        first_month = today.replace(day=1)
        last_day = _add_months(first_month, UPCOMING_MONTHS) - timedelta(days=1)
        buckets = {_month_key(_add_months(first_month, offset)): 0 for offset in range(UPCOMING_MONTHS)}
        for expiry in documents.expiry_dates_between(today, last_day):
            buckets[_month_key(expiry)] += 1

        by_group, ungrouped = self._by_location_or_department(ctx, documents, metrics["total"])

        active = documents.active()
        window_end = today + timedelta(days=90)
        expiring = list(
            self.db.scalars(
                active.where(Document.expiry_date >= today, Document.expiry_date <= window_end)
                .order_by(Document.expiry_date, Document.id)
                .limit(LIST_SIZE)
            )
        )
        expired = list(
            self.db.scalars(
                active.where(Document.expiry_date < today)
                .order_by(Document.expiry_date.desc(), Document.id)
                .limit(LIST_SIZE)
            )
        )
        recent = list(self.db.scalars(active.order_by(Document.created_at.desc(), Document.id).limit(5)))

        return Dashboard(
            today=today,
            expiring_window_days=expiring_window_days(),
            metrics=metrics,
            by_category=sorted(documents.counts_by_category(), key=lambda item: (-item[1], item[0])),
            upcoming_expirations=list(buckets.items()),
            by_group=by_group,
            ungrouped_count=ungrouped,
            expiring_documents=expiring,
            expired_documents=expired,
            recent_documents=recent,
            open_renewals=open_renewals,
            renewal_trend=[(month, started, completed) for month, (started, completed) in trend.items()],
        )

    def _by_location_or_department(
        self, ctx: OrgContext, documents: DocumentRepository, total: int
    ) -> tuple[list[GroupCount], int]:
        """Document counts per location or department.

        A document counts for the location or department it is attached to, or,
        when it is attached to an employee, supplier or piece of equipment, for
        that record's location (or else its department).
        """
        per_entity = documents.counts_by_entity()
        entities = EntityRepository(self.db, ctx.organization_id).get_many(list(per_entity))
        groups: dict[str, GroupCount] = {}
        grouped = 0
        for entity in entities:
            group: Entity | None = (
                entity if entity.type in ("location", "department") else entity.location or entity.department
            )
            if group is None:
                continue
            count = per_entity[entity.id]
            grouped += count
            key = str(group.id)
            existing = groups.get(key)
            groups[key] = GroupCount(key, group.name, group.type, count + (existing.count if existing else 0))
        ordered = sorted(groups.values(), key=lambda item: (-item.count, item.name))[:GROUP_LIMIT]
        return ordered, total - grouped

    def search(self, ctx: OrgContext, q: str, limit: int = 6) -> tuple[list[Document], list[Entity]]:
        documents = list(
            self.db.scalars(
                select(Document)
                .where(
                    Document.organization_id == ctx.organization_id,
                    Document.archived_at.is_(None),
                    _contains(Document.name, q)
                    | _contains(Document.document_number, q)
                    | _contains(Document.issuing_authority, q)
                    | _contains(Document.holder_name, q),
                )
                .order_by(Document.updated_at.desc())
                .limit(limit)
            )
        )
        entities = EntityRepository(self.db, ctx.organization_id).lookup(type_=None, q=q, limit=limit)
        return documents, entities
