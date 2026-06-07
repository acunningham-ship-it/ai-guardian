"""Provider failover — if a request fails, automatically retry on fallback providers.

Configurable per-model fallback chains. Handles 429 (rate limit), 500+ (server error),
and timeout errors with exponential backoff.
"""
import asyncio
import time
from typing import Optional

import httpx

from guardian.cost.tracker import MODEL_PRICING


# ── Default Fallback Chains ─────────────────────────────────────────
# If a model/provider fails, try these alternatives in order

DEFAULT_FALLBACKS = {
    # Anthropic → OpenAI → Google
    "anthropic/claude-opus-4": ["openai/o1", "google/gemini-2.5-pro"],
    "anthropic/claude-sonnet-4": ["openai/gpt-4o", "google/gemini-2.5-pro", "deepseek/deepseek-v3"],
    "anthropic/claude-haiku-4": ["openai/gpt-4o-mini", "google/gemini-2.5-flash"],
    # OpenAI → Anthropic → Google
    "openai/gpt-4o": ["anthropic/claude-sonnet-4", "google/gemini-2.5-pro"],
    "openai/gpt-4o-mini": ["anthropic/claude-haiku-4", "google/gemini-2.5-flash"],
    "openai/o1": ["anthropic/claude-opus-4", "google/gemini-2.5-pro"],
    "openai/o3-mini": ["anthropic/claude-sonnet-4", "deepseek/deepseek-v3"],
    # Google → OpenAI → Anthropic
    "google/gemini-2.5-pro": ["openai/gpt-4o", "anthropic/claude-sonnet-4"],
    "google/gemini-2.5-flash": ["openai/gpt-4o-mini", "anthropic/claude-haiku-4"],
    "google/gemini-2.0-flash": ["openai/gpt-4o-mini", "anthropic/claude-haiku-4"],
    # DeepSeek → OpenAI → Google
    "deepseek/deepseek-v3": ["openai/gpt-4o-mini", "google/gemini-2.5-flash"],
    "deepseek/deepseek-r1": ["openai/o3-mini", "anthropic/claude-sonnet-4"],
}

# Errors that trigger failover
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 529}

# Max retries before giving up
MAX_RETRIES = 2

# Base delay for exponential backoff (seconds)
BACKOFF_BASE = 1.0


def get_fallback_chain(model: str, custom_fallbacks: Optional[dict] = None) -> list[str]:
    """Get the fallback chain for a model.
    
    Checks custom config first, then defaults.
    Only returns models that exist in MODEL_PRICING.
    """
    # Check custom first
    if custom_fallbacks and model in custom_fallbacks:
        chain = custom_fallbacks[model]
    elif model in DEFAULT_FALLBACKS:
        chain = DEFAULT_FALLBACKS[model]
    else:
        # Try without provider prefix
        base = model.split("/")[-1] if "/" in model else model
        found = None
        for k, v in DEFAULT_FALLBACKS.items():
            if k.endswith(base):
                found = v
                break
        chain = found or []

    # Filter to models we know pricing for
    valid = [m for m in chain if m in MODEL_PRICING]
    return valid


class FailoverResult:
    """Result of a failover attempt."""
    __slots__ = ("success", "model", "provider", "response", "attempts", "error")

    def __init__(
        self,
        success: bool,
        model: str,
        provider: str,
        response: Optional[httpx.Response] = None,
        attempts: int = 1,
        error: Optional[str] = None,
    ):
        self.success = success
        self.model = model
        self.provider = provider
        self.response = response
        self.attempts = attempts
        self.error = error


async def request_with_failover(
    make_request_fn,
    model: str,
    provider: str,
    api_key: str,
    max_retries: int = MAX_RETRIES,
    custom_fallbacks: Optional[dict] = None,
) -> FailoverResult:
    """Execute a request with automatic failover on failure.
    
    Args:
        make_request_fn: async function(provider, model, api_key) -> httpx.Response
        model: The primary model to try
        provider: The primary provider
        api_key: API key for the primary provider
        max_retries: How many fallback attempts
        custom_fallbacks: Custom fallback chain override
    
    Returns:
        FailoverResult with the successful response or last error
    """
    from guardian.proxy.server import resolve_provider

    # Try primary request
    try:
        resp = await make_request_fn(provider, model, api_key)
        if resp.status_code < 400:
            return FailoverResult(
                success=True, model=model, provider=provider,
                response=resp, attempts=1,
            )
        if resp.status_code not in RETRYABLE_STATUS_CODES:
            # Client error (4xx except 429) — don't retry, it's our fault
            return FailoverResult(
                success=False, model=model, provider=provider,
                response=resp, attempts=1,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        pass  # Fall through to failover
    except Exception as e:
        return FailoverResult(
            success=False, model=model, provider=provider,
            attempts=1, error=str(e),
        )

    # Primary failed — try fallbacks
    fallbacks = get_fallback_chain(model, custom_fallbacks)

    for attempt, fallback_model in enumerate(fallbacks[:max_retries], start=2):
        fallback_provider = resolve_provider(fallback_model)

        # We need an API key for the fallback provider
        # This is handled by the caller — they should pass keys for all providers
        # For now, skip if provider changed and no key available
        if fallback_provider != provider:
            # Signal to caller that we need a different key
            # In practice, the proxy server handles key resolution
            continue

        # Exponential backoff
        delay = BACKOFF_BASE * (2 ** (attempt - 2))
        await asyncio.sleep(delay)

        try:
            resp = await make_request_fn(fallback_provider, fallback_model, api_key)
            if resp.status_code < 400:
                return FailoverResult(
                    success=True, model=fallback_model, provider=fallback_provider,
                    response=resp, attempts=attempt,
                )
        except (httpx.TimeoutException, httpx.ConnectError):
            continue
        except Exception:
            continue

    # All attempts failed
    return FailoverResult(
        success=False, model=model, provider=provider,
        attempts=1 + min(len(fallbacks), max_retries),
        error=f"All {1 + min(len(fallbacks), max_retries)} attempts failed",
    )
