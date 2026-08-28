"""Model access. The only module that knows which provider is in play.

Everything else names a model with a provider-prefixed string ("openai:gpt-4o-mini",
"anthropic:claude-...") and never imports a provider SDK.
"""

from __future__ import annotations

from functools import lru_cache

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from pr4docs.config import get_settings

RETRY_ATTEMPTS = 3
"""Transient 429s and 5xxs, not bad prompts. Applied per role after the structured
output is bound, since with_retry returns a Runnable that no longer offers binding."""


@lru_cache
def get_model(model: str | None = None) -> BaseChatModel:
    """Temperature 0 throughout: an edit agent that returns different text for the same
    document and request is not debuggable, and the retry loop needs a failure to be
    reproducible before it can be corrected."""
    settings = get_settings()
    return init_chat_model(model or settings.model, temperature=0)
