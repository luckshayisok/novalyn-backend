"""Document-reading providers."""

from functools import lru_cache

from app.core.config import get_settings
from app.integrations.ai.base import AIProvider


@lru_cache
def get_ai_provider() -> AIProvider | None:
    """The configured provider, or None when automatic reading is switched off."""
    settings = get_settings()
    if settings.ai_provider == "anthropic":
        from app.integrations.ai.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    if settings.ai_provider == "heuristic":
        from app.integrations.ai.heuristic import HeuristicProvider

        return HeuristicProvider()
    return None
