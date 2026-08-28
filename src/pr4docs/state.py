"""Graph state.

Every value here is checkpointed and must stay JSON-serializable — the graph pauses
for human approval, the process may die, and this is all that survives. In particular
no open document handles: only paths.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

Status = Literal["planning", "awaiting_approval", "finalized", "failed"]


class PR4DocsState(TypedDict, total=False):
    source_path: str
    """The upload. Never mutated — every attempt re-applies from here, so a failed or
    rejected proposal needs no rollback, just a discarded working file."""

    working_path: str | None
    """Where the current proposal lives as a .docx carrying tracked changes."""

    output_path: str | None
    request: str

    outline: list[dict[str, Any]]
    plan: list[dict[str, Any]]
    steps: list[dict[str, Any]]

    preview_failures: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    diff: str
    validation: dict[str, Any] | None

    revise_feedback: str | None
    """Why the last attempt was rejected — by preview, by the validator, or by the user."""

    attempts: int
    """Automated retries in the current proposal cycle. A human rejection resets this:
    the cap exists to stop runaway LLM loops, and a person clicking reject is not one."""

    approved: bool | None
    status: Status
    errors: Annotated[list[str], operator.add]


def initial_state(source_path: str, request: str) -> PR4DocsState:
    return PR4DocsState(
        source_path=source_path,
        working_path=None,
        output_path=None,
        request=request,
        outline=[],
        plan=[],
        steps=[],
        preview_failures=[],
        changes=[],
        diff="",
        validation=None,
        revise_feedback=None,
        attempts=0,
        approved=None,
        status="planning",
        errors=[],
    )
