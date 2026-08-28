"""Role behaviour that does not need a model: prompt assembly and result mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pr4docs.docs.superdoc import Block, Change
from pr4docs.roles import (
    PLANNER_PREVIEW_CHARS,
    LLMComposer,
    LLMPlanner,
    LLMValidator,
    _EditPlanOut,
    _PlannedEditOut,
    _render_outline,
    _ValidationOut,
)


@dataclass
class StubResponse:
    content: str


@dataclass
class StubModel:
    """Stands in for a chat model: records prompts, returns whatever it was given."""

    result: Any = None
    prompts: list[list[tuple[str, str]]] = field(default_factory=list)

    def with_structured_output(self, schema: type) -> StubModel:
        return self

    def with_retry(self, **kwargs: Any) -> StubModel:
        return self

    def invoke(self, messages: list[tuple[str, str]]) -> Any:
        self.prompts.append(messages)
        return self.result


OUTLINE = [
    Block("00000002", "heading", 1, "Introduction", heading_level=2),
    Block("00000003", "paragraph", 2, "A" * (PLANNER_PREVIEW_CHARS + 50)),
]


def test_outline_is_truncated_for_the_planner():
    rendered = _render_outline(OUTLINE)

    assert "[00000002] (h2) Introduction" in rendered
    assert rendered.endswith("…")
    assert len(rendered) < 2 * PLANNER_PREVIEW_CHARS


def test_planner_takes_node_type_from_the_outline_not_the_model():
    model = StubModel(
        _EditPlanOut(
            edits=[
                _PlannedEditOut(node_id="00000002", op="text.rewrite", instruction="Retitle it."),
                _PlannedEditOut(node_id="00000003", op="text.delete", instruction="Drop it."),
            ]
        )
    )
    edits = LLMPlanner(model)(request="tidy up", outline=OUTLINE, feedback=None)

    assert [e.step_id for e in edits] == ["s1", "s2"]
    assert [e.node_type for e in edits] == ["heading", "paragraph"]
    assert [e.op for e in edits] == ["text.rewrite", "text.delete"]


def test_planner_passes_an_unknown_node_id_through_to_preview():
    """Preview owns rejection, so a hallucinated id must survive this far to be reported."""
    model = StubModel(
        _EditPlanOut(edits=[_PlannedEditOut(node_id="ghost", op="text.rewrite", instruction="x")])
    )
    edits = LLMPlanner(model)(request="tidy up", outline=OUTLINE, feedback=None)

    assert edits[0].node_id == "ghost"
    assert edits[0].node_type == "paragraph"


def test_planner_includes_rejection_feedback_in_the_prompt():
    model = StubModel(_EditPlanOut(edits=[]))
    LLMPlanner(model)(request="tidy up", outline=OUTLINE, feedback="TARGET_NOT_FOUND on s1")

    user_message = model.prompts[0][-1][1]
    assert "TARGET_NOT_FOUND on s1" in user_message
    assert "Produce a different plan" in user_message


def test_composer_sees_the_full_block_text_and_strips_the_reply():
    model = StubModel(StubResponse("  A tighter paragraph.\n"))
    text = LLMComposer(model)(
        request="shorten it", instruction="Trim to one sentence.", current_text="Long original."
    )

    assert text == "A tighter paragraph."
    assert "Long original." in model.prompts[0][-1][1]


def test_validator_fails_without_calling_the_model_when_nothing_changed():
    model = StubModel(None)
    verdict = LLMValidator(model)(request="shorten it", changes=[])

    assert not verdict.passed
    assert model.prompts == []


def test_validator_maps_the_models_verdict():
    model = StubModel(_ValidationOut(passed=False, reason="only 5% shorter"))
    changes = [Change("c1", "replacement", "before text", "after text", "00000003", "CLI")]

    verdict = LLMValidator(model)(request="make it 40% shorter", changes=changes)

    assert not verdict.passed
    assert verdict.reason == "only 5% shorter"
    assert "BEFORE: before text" in model.prompts[0][-1][1]
