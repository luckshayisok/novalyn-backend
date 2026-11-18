"""Plans and what each one includes.

This is the one place that says what a plan allows. The billing service reads
it to answer "may this organization do X?", and the plans page shows it.
Prices here are for display; what a customer is charged is set in the payment
provider's dashboard (Stripe prices, Razorpay plans) and linked through the
`STRIPE_PRICE_*` and `RAZORPAY_PLAN_*` settings.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Plan(StrEnum):
    FREE = "free"
    STARTER = "starter"
    GROWTH = "growth"
    BUSINESS = "business"


class Feature(StrEnum):
    AUTOMATED_REMINDERS = "automated_reminders"  # reminders and alerts are sent at all
    CUSTOM_REMINDERS = "custom_reminders"  # choose the reminder days
    BASIC_REPORTS = "basic_reports"
    ADVANCED_REPORTS = "advanced_reports"
    PDF_EXPORT = "pdf_export"
    CSV_EXPORT = "csv_export"
    DOCUMENT_HISTORY = "document_history"  # download earlier versions of a file
    ENTITY_MANAGEMENT = "entity_management"
    MULTIPLE_LOCATIONS = "multiple_locations"
    ADVANCED_PERMISSIONS = "advanced_permissions"  # Compliance Manager and Viewer roles
    CUSTOM_INTEGRATIONS = "custom_integrations"
    PRIORITY_SUPPORT = "priority_support"


FEATURE_LABELS: dict[Feature, str] = {
    Feature.AUTOMATED_REMINDERS: "Automated expiry reminders",
    Feature.CUSTOM_REMINDERS: "Custom reminder schedule",
    Feature.BASIC_REPORTS: "Basic reports",
    Feature.ADVANCED_REPORTS: "Advanced reports",
    Feature.PDF_EXPORT: "PDF export",
    Feature.CSV_EXPORT: "CSV export",
    Feature.DOCUMENT_HISTORY: "Document version history",
    Feature.ENTITY_MANAGEMENT: "Employees, suppliers, equipment and departments",
    Feature.MULTIPLE_LOCATIONS: "Multiple locations",
    Feature.ADVANCED_PERMISSIONS: "Advanced permissions (Compliance Manager and Viewer roles)",
    Feature.CUSTOM_INTEGRATIONS: "Custom integrations",
    Feature.PRIORITY_SUPPORT: "Priority support",
}

UNLIMITED = None


@dataclass(frozen=True)
class PlanSpec:
    plan: Plan
    name: str
    description: str
    max_documents: int | None
    max_members: int | None
    max_locations: int | None
    features: frozenset[Feature]
    # Display prices per month and per year, in the smallest unit of each currency. None: contact sales.
    monthly_price: dict[str, int] | None = field(default=None)
    yearly_price: dict[str, int] | None = field(default=None)


_STARTER = frozenset(
    {Feature.AUTOMATED_REMINDERS, Feature.BASIC_REPORTS, Feature.PDF_EXPORT, Feature.DOCUMENT_HISTORY}
)
_GROWTH = _STARTER | {
    Feature.CUSTOM_REMINDERS,
    Feature.ADVANCED_REPORTS,
    Feature.CSV_EXPORT,
    Feature.ENTITY_MANAGEMENT,
}
_BUSINESS = _GROWTH | {
    Feature.MULTIPLE_LOCATIONS,
    Feature.ADVANCED_PERMISSIONS,
    Feature.CUSTOM_INTEGRATIONS,
    Feature.PRIORITY_SUPPORT,
}

PLANS: dict[Plan, PlanSpec] = {
    Plan.FREE: PlanSpec(
        Plan.FREE,
        "Free",
        "For trying NOVALYN with a handful of documents.",
        max_documents=10,
        max_members=1,
        max_locations=1,
        # Free keeps basic reminders: alerts at the default times, no custom schedule.
        features=frozenset({Feature.AUTOMATED_REMINDERS}),
        monthly_price={"USD": 0, "INR": 0},
        yearly_price={"USD": 0, "INR": 0},
    ),
    Plan.STARTER: PlanSpec(
        Plan.STARTER,
        "Starter",
        "For small teams tracking their core licenses and certificates.",
        max_documents=100,
        max_members=3,
        max_locations=1,
        features=_STARTER,
        monthly_price={"USD": 1900, "INR": 149900},
        yearly_price={"USD": 19000, "INR": 1499000},
    ),
    Plan.GROWTH: PlanSpec(
        Plan.GROWTH,
        "Growth",
        "For growing businesses managing suppliers, staff and equipment.",
        max_documents=500,
        max_members=10,
        max_locations=1,
        features=_GROWTH,
        monthly_price={"USD": 5900, "INR": 499900},
        yearly_price={"USD": 59000, "INR": 4999000},
    ),
    Plan.BUSINESS: PlanSpec(
        Plan.BUSINESS,
        "Business",
        "For multi-site organizations with custom needs.",
        max_documents=UNLIMITED,
        max_members=UNLIMITED,
        max_locations=UNLIMITED,
        features=_BUSINESS,
    ),
}

PLAN_ORDER = [Plan.FREE, Plan.STARTER, Plan.GROWTH, Plan.BUSINESS]
SELF_SERVE_PLANS = (Plan.STARTER, Plan.GROWTH)


def first_plan_with(feature: Feature) -> PlanSpec:
    """The cheapest plan that includes a feature, named in "upgrade to ..." messages."""
    return next(PLANS[plan] for plan in PLAN_ORDER if feature in PLANS[plan].features)
