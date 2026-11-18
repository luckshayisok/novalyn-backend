"""Plans, subscription, checkout and payment-provider webhooks."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_org_context, require_permission
from app.core.database import get_db
from app.core.permissions import Permission
from app.core.rate_limit import rate_limit
from app.schemas.billing import BillingOut, CheckoutRequest, CheckoutResponse, PlanOut, plan_catalog
from app.schemas.common import Message
from app.services.billing import BillingService, InvalidWebhookError
from app.services.context import OrgContext

billing_router = APIRouter(prefix="/organizations/{organization_id}/billing", tags=["billing"])
public_billing_router = APIRouter(prefix="/billing", tags=["billing"])

MAX_WEBHOOK_BYTES = 512 * 1024


@billing_router.get("", response_model=BillingOut)
def billing_overview(ctx: OrgContext = Depends(get_org_context), db: Session = Depends(get_db)) -> BillingOut:
    """The plan, what it allows, what is used, and the billing history. Every member may see it."""
    return BillingOut.build(BillingService(db).overview(ctx))


@billing_router.post(
    "/checkout", response_model=CheckoutResponse, dependencies=[Depends(rate_limit("checkout", 10, 3600))]
)
def start_checkout(
    body: CheckoutRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BILLING_MANAGE)),
    db: Session = Depends(get_db),
) -> CheckoutResponse:
    """Start paying for a plan on the provider's own page.

    The plan changes only once the provider confirms the payment.
    """
    url = BillingService(db).start_checkout(
        ctx, plan=body.plan, interval=body.interval, provider_name=body.provider
    )
    return CheckoutResponse(url=url)


@billing_router.post("/cancel", response_model=Message)
def cancel_subscription(
    ctx: OrgContext = Depends(require_permission(Permission.BILLING_MANAGE)), db: Session = Depends(get_db)
) -> Message:
    subscription = BillingService(db).cancel(ctx)
    until = subscription.current_period_end
    when = f" on {until:%d %B %Y}" if until else " at the end of the paid period"
    return Message(message=f"Your subscription will end{when}. Until then, nothing changes.")


@public_billing_router.get("/plans", response_model=list[PlanOut])
def plans() -> list[PlanOut]:
    """Every plan and what it includes. Public, for the pricing page."""
    return plan_catalog()


@public_billing_router.post(
    "/webhooks/{provider}",
    response_model=Message,
    dependencies=[Depends(rate_limit("billing-webhook", 600, 60))],
)
async def provider_webhook(
    provider: Literal["stripe", "razorpay"], request: Request, db: Session = Depends(get_db)
) -> Message:
    """Events from Stripe or Razorpay. Accepted only with a valid signature; each event is applied once."""
    body = await request.body()  # the exact bytes are needed to check the signature
    if len(body) > MAX_WEBHOOK_BYTES:
        raise InvalidWebhookError("The webhook body is too large.")
    headers = {key.lower(): value for key, value in request.headers.items()}
    outcome = await run_in_threadpool(BillingService(db).handle_webhook, provider, headers, body)
    return Message(message=outcome)
