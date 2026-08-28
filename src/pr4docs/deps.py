"""The seams the graph is built against.

The three LLM roles and the document opener are injected, so the state machine can be
tested end to end without an API key or an editor subprocess.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pr4docs.config import Settings, get_settings
from pr4docs.docs.superdoc import Block, Change, DocumentSession, StepOp, open_document


@dataclass(frozen=True)
class PlannedEdit:
    """Which block to change and what to do to it — never the final prose."""

    step_id: str
    node_id: str
    node_type: str
    instruction: str
    op: StepOp = "text.rewrite"


@dataclass(frozen=True)
class Validation:
    passed: bool
    reason: str


class EditPlanner(Protocol):
    def __call__(
        self, *, request: str, outline: list[Block], feedback: str | None
    ) -> list[PlannedEdit]: ...


class TextComposer(Protocol):
    def __call__(
        self,
        *,
        request: str,
        instruction: str,
        current_text: str,
        feedback: str | None,
        previous_text: str | None,
    ) -> str:
        """`feedback` and `previous_text` are set when a prior attempt was rejected.

        A validator rejection is a complaint about the prose, so it has to reach the
        component that wrote the prose. Routing it only to the planner cannot converge:
        the planner re-emits the same instruction and the composer repeats itself.
        """
        ...


class ResultValidator(Protocol):
    def __call__(self, *, request: str, changes: list[Change]) -> Validation: ...


DocumentOpener = Callable[[Path], AbstractContextManager[DocumentSession]]


@dataclass(frozen=True)
class Deps:
    planner: EditPlanner
    composer: TextComposer
    validator: ResultValidator
    open_document: DocumentOpener = open_document
    settings: Settings = field(default_factory=get_settings)


__all__ = [
    "Deps",
    "DocumentOpener",
    "EditPlanner",
    "PlannedEdit",
    "ResultValidator",
    "TextComposer",
    "Validation",
]
