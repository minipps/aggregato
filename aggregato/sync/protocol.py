"""The JSON-lines protocol spoken across the child-to-parent pipe (research.md ).

One message per line, each a tagged object. This is the boundary where plugin-controlled output
becomes host data, so the parent validates every line against these models and treats an unparseable
line as a provider failure rather than as noise to skip.

JSON lines rather than pickle, deliberately: the payload crosses a boundary from code the host does
not trust, and unpickling arbitrary data from a plugin is remote code execution by design. It is
also the same shape the recorded fixtures use, so there is one wire format for production and tests
.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from aggregato.domain.enums import ErrorClass
from aggregato.domain.models import CheckResult, Cursor, NormalizedBatch, RawRecord

#: Cap on one line's length. A provider that emits an unbounded line would otherwise let the parent
#: read until it runs out of memory — which is a hang the wall-clock kill cannot help with, because
#: the child looks busy rather than stuck.
MAX_LINE_BYTES = 8 * 1024 * 1024


class BatchMessage(BaseModel):
    """One record and what ``normalize`` made of it.

    Both halves travel together because the parent needs the raw payload for
    ``provider_items.raw_payload`` — the replay source  — and the normalized batch to write.
    Sending them separately would let a crash split a record from its own payload.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["batch"] = "batch"
    raw: RawRecord
    batch: NormalizedBatch


class CheckpointMessage(BaseModel):
    """A resume point. The parent persists the cursor so a later failure resumes here ."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["checkpoint"] = "checkpoint"
    cursor: Cursor


class ErrorMessage(BaseModel):
    """The child reporting a classified failure before it exits.

    The child classifies rather than the parent guessing from an exit code, because only the child
    saw the exception. The parent still decides retry policy (contract §4).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["error"] = "error"
    error_class: ErrorClass
    message: str
    #: Tail of the traceback, for the run's ``log_excerpt``. Never an operator-facing message.
    detail: str | None = None
    #: Platform-provided delay in seconds, when this was a rate limit.
    retry_after_seconds: float | None = Field(default=None, ge=0)


class ResponseMessage(BaseModel):
    """A bounded host-client response snapshot for operator diagnosis."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["response"] = "response"
    method: str
    url: str
    status: int
    headers: dict[str, str]
    body: str


class FailureMessage(BaseModel):
    """One record the child could not normalize. The run continues .

    Carries the payload so the parent can store it for replay. Distinct from ``ErrorMessage``:
    that one ends the run, this one is a single poisoned record.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["failure"] = "failure"
    native_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    error: str


class CheckMessage(BaseModel):
    """The one diagnostic result emitted by a credential-check child run."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["check"] = "check"
    result: CheckResult


ChildMessage = Annotated[
    BatchMessage
    | CheckpointMessage
    | ErrorMessage
    | FailureMessage
    | CheckMessage
    | ResponseMessage,
    Field(discriminator="type"),
]

#: Validates one line. The discriminator makes an unknown ``type`` a validation error rather than a
#: silently ignored message — a provider protocol we half-understand is worse than one we reject.
MESSAGE_ADAPTER: TypeAdapter[ChildMessage] = TypeAdapter(ChildMessage)


class ProtocolViolation(ValueError):
    """A line from the child was not a valid message.

    Raised in the parent. Classified as ``internal``: the provider's output is malformed, which is a
    bug in the provider or in the host, not a platform problem.
    """


def encode(message: ChildMessage) -> str:
    """Serialize one message to a single line, newline included.

    Pydantic's JSON serializer is used rather than ``json.dumps`` because it round-trips ``Decimal``
    and aware ``datetime`` exactly, which a rating of ``3.5`` and a logged timestamp both depend on.
    """
    line = message.model_dump_json()
    if "\n" in line:  # pragma: no cover - defensive; JSON serialization never emits a raw newline
        raise ProtocolViolation("a message serialized to more than one line")
    return line + "\n"


def decode(line: str | bytes) -> ChildMessage:
    """Validate one line from the child.

    Args:
        line: One line of the child's stdout, with or without its trailing newline.

    Returns:
        The parsed message.

    Raises:
        ProtocolViolation: The line is too long, is not JSON, or is not a known message shape.
    """
    raw = line.encode() if isinstance(line, str) else line
    if len(raw) > MAX_LINE_BYTES:
        raise ProtocolViolation(
            f"child sent a {len(raw)} byte line, over the {MAX_LINE_BYTES} byte limit"
        )
    try:
        return MESSAGE_ADAPTER.validate_json(raw)
    except ValueError as exc:
        raise ProtocolViolation(f"child sent an invalid message: {exc}") from exc
