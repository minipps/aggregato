"""Provider exceptions and their error classifications.

The host catches ``ProviderError`` during provider operations and reads ``error_class`` from the
exception. Unrecognized exceptions are classified as ``internal``.
"""

from __future__ import annotations

from typing import ClassVar

from aggregato.domain.enums import ErrorClass


class ProviderError(Exception):
    """Base for every failure a provider signals deliberately.

    Subclasses set ``error_class``; the host records it on the run and on ``providers.last_error``.
    """

    error_class: ClassVar[ErrorClass] = ErrorClass.INTERNAL


class AuthError(ProviderError):
    """Credentials are missing, wrong, or no longer accepted.

    Host behaviour: **no retry**, provider goes to ``degraded`` immediately — retrying a rejected
    credential risks locking the operator out of their own account.
    """

    error_class: ClassVar[ErrorClass] = ErrorClass.AUTH


class BlockedError(ProviderError):
    """The platform is refusing access — a block page, a CAPTCHA, an anti-bot challenge.

    Host behaviour: **no retry**, ``degraded`` immediately, because retrying deepens the block. This
    is also the only correct response to a CAPTCHA: circumventing one is a hard line, not a
    configurable default.
    """

    error_class: ClassVar[ErrorClass] = ErrorClass.BLOCKED


class TransportError(ProviderError):
    """The provider endpoint could not be reached after the host's bounded retries."""

    error_class: ClassVar[ErrorClass] = ErrorClass.TRANSPORT


class StructureChangedError(ProviderError):
    """The payload no longer looks like what this provider was written against.

    Host behaviour: **no retry**; the UI says "this provider needs updating" with an issue-tracker
    link. Raised instead of returning empty, because silence plus delete inference is how an archive
    gets erased.
    """

    error_class: ClassVar[ErrorClass] = ErrorClass.STRUCTURE_CHANGED


class RateLimited(ProviderError):
    """The platform asked us to slow down.

    Host behaviour: retries, honours ``retry_after``, and lengthens the effective interval for the
    rest of the session.

    Args:
        retry_after: Seconds to wait, when the platform said. ``None`` means it did not, and the
            host falls back to its own retry ladder rather than inventing a number.
    """

    error_class: ClassVar[ErrorClass] = ErrorClass.RATE_LIMIT

    def __init__(self, *args: object, retry_after: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after = retry_after
