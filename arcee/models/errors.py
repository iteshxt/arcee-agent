"""Failures a model role can raise. The orchestrator decides per type: fall back or fail."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arcee.models.base import TokenUsage
    from arcee.models.jsonfix import ReplyError


class ModelError(Exception):
    """Base for failures the orchestrator handles per role (fallback, failed step)."""


class ModelUnavailableError(ModelError):
    """The provider could not be reached or kept refusing after backoff. Try the fallback."""


class QuotaExhaustedError(ModelUnavailableError):
    """The model's requests-per-day budget is spent until the provider's quota resets."""


class RetryableError(ModelError):
    """One round-trip failed in a way that may succeed if repeated. Retried by the base class."""

    def __init__(self, reason: str, retry_after_s: float | None = None) -> None:
        super().__init__(reason + (f", retry after {retry_after_s}s" if retry_after_s else ""))
        self.retry_after_s = retry_after_s


class RateLimitedError(RetryableError):
    """HTTP 429."""

    def __init__(self, retry_after_s: float | None = None) -> None:
        super().__init__("rate limited", retry_after_s)


class ServerBusyError(RetryableError):
    """HTTP 5xx or a dropped connection: the provider failed, not the request."""


class ModelOutputError(ModelError):
    """The model did not produce a valid object within the retry budget."""

    def __init__(self, error: ReplyError, raw: str, attempts: int, usage: TokenUsage) -> None:
        super().__init__(f"invalid reply after {attempts} attempts: {error}")
        self.error = error
        self.raw = raw
        self.attempts = attempts
        self.usage = usage
