"""Real implementations of the three LLM roles the graph depends on.

The split between planner and composer is the point: the planner reads a *truncated*
outline and only decides where to edit, while the composer sees one block's full text
and writes only that block. Neither is asked to do both at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeVar

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from pr4docs.deps import Deps, PlannedEdit, Validation
from pr4docs.docs.superdoc import Block, Change, StepOp
from pr4docs.llm import RETRY_ATTEMPTS, get_model

T = TypeVar("T")

PLANNER_PREVIEW_CHARS = 240
"""The planner decides *where*, not *what*, so it gets previews rather than full text.
On a long document this is the difference between a cheap call and an unaffordable one."""


class _PlannedEditOut(BaseModel):
    node_id: str = Field(
        description="node_id of the block to edit, copied exactly from the outline"
    )
    op: Literal["text.rewrite", "text.delete"]
    instruction: str = Field(
        description="What to do to this block, in one sentence. Not the replacement text."
    )


class _EditPlanOut(BaseModel):
    edits: list[_PlannedEditOut] = Field(
        description="Only the blocks that must change. Empty if the request cannot be met."
    )


class _ValidationOut(BaseModel):
    passed: bool
    reason: str = Field(description="One sentence. If it failed, say concretely what is wrong.")


PLANNER_SYSTEM = """You plan edits to a Word document. You decide WHICH blocks change \
and WHAT should happen to each. You never write the replacement text — a separate step \
does that.

Rules:
- Use node_id values exactly as they appear in the outline. Never invent one.
- Only include blocks that genuinely must change.
- Prefer editing body paragraphs over headings unless the request is about a heading.
- Each instruction must be self-contained: the writer sees only that block's text and \
your instruction, not the rest of the document."""

COMPOSER_SYSTEM = """You rewrite a single block of a Word document.

Return ONLY the replacement text. No preamble, no quotes, no markdown, no explanation. \
Preserve the original's voice and factual content unless the instruction says otherwise. \
Write plain prose: the text goes into a Word paragraph that keeps its own formatting."""

VALIDATOR_SYSTEM = """You check whether a set of edits actually satisfies what the user \
asked for.

You see the request and the before/after text of each change. Judge only whether the \
request was met. Do not judge style you were not asked about. If the request specified \
something measurable (a length, a tone, a removal), check that specifically. Be strict: \
passing a change that ignores the request wastes the reviewer's time."""


def _as_op(op: str) -> StepOp:
    return "text.delete" if op == "text.delete" else "text.rewrite"


def _expect(value: object, schema: type[T]) -> T:
    """with_structured_output returns the pydantic instance, but a provider that hands
    back a raw dict would otherwise fail much later with an opaque AttributeError."""
    if not isinstance(value, schema):
        raise TypeError(f"expected {schema.__name__} from the model, got {type(value).__name__}")
    return value


def _render_outline(blocks: list[Block]) -> str:
    lines = []
    for b in blocks:
        label = f"h{b.heading_level}" if b.is_heading else b.node_type
        text = b.text[:PLANNER_PREVIEW_CHARS]
        if len(b.text) > PLANNER_PREVIEW_CHARS:
            text += "…"
        lines.append(f"[{b.node_id}] ({label}) {text}")
    return "\n".join(lines)


def _render_changes(changes: list[Change]) -> str:
    return "\n\n".join(
        f"--- change {i + 1} ---\nBEFORE: {c.before}\nAFTER: {c.after}"
        for i, c in enumerate(changes)
    )


@dataclass
class LLMPlanner:
    model: BaseChatModel = field(default_factory=get_model)

    def __call__(
        self, *, request: str, outline: list[Block], feedback: str | None
    ) -> list[PlannedEdit]:
        by_id = {b.node_id: b for b in outline}
        chain = self.model.with_structured_output(_EditPlanOut).with_retry(
            stop_after_attempt=RETRY_ATTEMPTS
        )

        user = f"REQUEST:\n{request}\n\nDOCUMENT OUTLINE:\n{_render_outline(outline)}"
        if feedback:
            user += (
                f"\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED:\n{feedback}\n"
                "Produce a different plan that addresses this."
            )

        result = _expect(chain.invoke([("system", PLANNER_SYSTEM), ("user", user)]), _EditPlanOut)

        return [
            PlannedEdit(
                step_id=f"s{i + 1}",
                node_id=e.node_id,
                # taken from the outline, not the model — one less thing to hallucinate.
                # an unknown node_id survives to preview on purpose, which rejects it
                # with a reason the planner can act on.
                node_type=by_id[e.node_id].node_type if e.node_id in by_id else "paragraph",
                instruction=e.instruction,
                op=_as_op(e.op),
            )
            for i, e in enumerate(result.edits)
        ]


@dataclass
class LLMComposer:
    model: BaseChatModel = field(default_factory=get_model)

    def __call__(self, *, request: str, instruction: str, current_text: str) -> str:
        chain = self.model.with_retry(stop_after_attempt=RETRY_ATTEMPTS)
        user = (
            f"OVERALL REQUEST (for context):\n{request}\n\n"
            f"INSTRUCTION FOR THIS BLOCK:\n{instruction}\n\n"
            f"CURRENT TEXT:\n{current_text}"
        )
        response = chain.invoke([("system", COMPOSER_SYSTEM), ("user", user)])
        return str(response.content).strip()


@dataclass
class LLMValidator:
    model: BaseChatModel = field(default_factory=get_model)

    def __call__(self, *, request: str, changes: list[Change]) -> Validation:
        if not changes:
            return Validation(False, "no changes were produced")

        chain = self.model.with_structured_output(_ValidationOut).with_retry(
            stop_after_attempt=RETRY_ATTEMPTS
        )
        user = f"REQUEST:\n{request}\n\nCHANGES:\n{_render_changes(changes)}"
        result = _expect(
            chain.invoke([("system", VALIDATOR_SYSTEM), ("user", user)]), _ValidationOut
        )
        return Validation(passed=result.passed, reason=result.reason)


def build_deps() -> Deps:
    """The production wiring: real models, real documents."""
    model = get_model()
    return Deps(
        planner=LLMPlanner(model),
        composer=LLMComposer(model),
        validator=LLMValidator(model),
    )
