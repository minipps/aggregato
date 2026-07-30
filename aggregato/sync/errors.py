"""Classifying a failure, and the set of classes that must never be retried.

Providers raise (contract §4); the host classifies the exception into an ``ErrorClass`` and the
retry ladder decides from that class alone. Provider exceptions carry their own ``error_class``, so
for those classification is a lookup. Everything else is either a transport failure of the
host-owned HTTP client or, by definition, ``internal``.

This module may import ``httpx``: the import-linter contract fences ``aggregato.providers``, not
``aggregato.sync``, and classifying "the network broke" needs the client's exception tree.
"""

from __future__ import annotations

import httpx

from aggregato.domain.enums import ErrorClass
from aggregato.providers.errors import ProviderError

NEVER_RETRY: frozenset[ErrorClass] = frozenset(
    {
        # Retrying a rejected credential risks locking the operator out of their own account.
        ErrorClass.AUTH,
        # Retrying deepens the block, and circumventing a challenge is a hard line (FR-044).
        ErrorClass.BLOCKED,
        # The provider code itself needs updating; no amount of waiting fixes it (FR-024).
        ErrorClass.STRUCTURE_CHANGED,
    }
)
"""The never-retry set (FR-021). These skip the ladder entirely and go straight to ``degraded``."""

_SERVER_ERROR_FLOOR = 500

ACTION_REQUIRED: dict[ErrorClass, str] = {
    ErrorClass.AUTH: "Update this provider's credentials, then trigger a sync.",
    ErrorClass.BLOCKED: "Resolve the platform access block; do not retry until access is restored.",
    ErrorClass.STRUCTURE_CHANGED: (
        "This provider needs updating for the platform's changed response."
    ),
    ErrorClass.RATE_LIMIT: (
        "The platform asked us to slow down; Aggregato will retry automatically."
    ),
    ErrorClass.TRANSPORT: (
        "The platform or network is temporarily unavailable; Aggregato will retry automatically."
    ),
    ErrorClass.PARSE: "Inspect the provider response; Aggregato will retry automatically.",
    ErrorClass.INTERNAL: "Inspect the run details and logs; Aggregato will retry automatically.",
}


def action_required(error_class: ErrorClass) -> str:
    """Return a specific, operator-facing next action for every error class (SC-005)."""
    return ACTION_REQUIRED[error_class]


def classify(exc: BaseException) -> ErrorClass:
    """Map an exception to the ``ErrorClass`` recorded on the run and on ``providers.last_error``.

    Args:
        exc: Anything raised while fetching or normalizing. Provider exceptions are authoritative
            about their own class; host-side exceptions are classified here.

    Returns:
        The class from contract §4. Never raises and never returns ``None``: an unrecognised
        exception is ``internal``, which takes the ladder and keeps its traceback in the run's
        ``log_excerpt``, rather than being silently dropped.
    """
    if isinstance(exc, ProviderError):
        return exc.error_class
    if isinstance(exc, httpx.HTTPStatusError):
        # A 5xx is the platform failing, not us; 4xx that a provider did not turn into AuthError or
        # BlockedError is a provider bug, so it stays `internal` and is visible as one.
        if exc.response.status_code >= _SERVER_ERROR_FLOOR:
            return ErrorClass.TRANSPORT
        return ErrorClass.INTERNAL
    if isinstance(exc, httpx.TransportError):
        return ErrorClass.TRANSPORT
    return ErrorClass.INTERNAL


def schedules_retry(error_class: ErrorClass) -> bool:
    """Whether a failure of this class gets another attempt at all.

    Args:
        error_class: The classification of the failure that just happened.

    Returns:
        ``False`` for every member of :data:`NEVER_RETRY`, ``True`` otherwise. A ``False`` here
        means the provider goes ``degraded`` with no ``next_run_at`` — the operator has to act.
    """
    return error_class not in NEVER_RETRY
