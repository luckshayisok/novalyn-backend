"""Payment providers. Each is available only when its keys are configured."""

from app.core.config import get_settings
from app.integrations.payments.base import PaymentProvider


def all_providers() -> dict[str, PaymentProvider]:
    from app.integrations.payments.razorpay_provider import RazorpayProvider
    from app.integrations.payments.stripe_provider import StripeProvider

    settings = get_settings()
    return {"stripe": StripeProvider(settings), "razorpay": RazorpayProvider(settings)}


def configured_providers() -> dict[str, PaymentProvider]:
    return {name: provider for name, provider in all_providers().items() if provider.is_configured()}
