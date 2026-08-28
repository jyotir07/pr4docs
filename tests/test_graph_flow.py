"""Proves the state machine — every branch, retry, and the approval pause — with no
LLM and no editor subprocess."""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from pr4docs.config import Settings
from pr4docs.deps import Deps
from pr4docs.graph import build_graph
from pr4docs.state import initial_state
from tests.fakes import (
    FakeDocumentStore,
    ScriptedComposer,
    ScriptedPlanner,
    ScriptedValidator,
    edit,
    failing,
    passing,
)

SOURCE = "uploads/job.docx"


@pytest.fixture
def store() -> FakeDocumentStore:
    s = FakeDocumentStore()
    s.seed(Path(SOURCE))
    return s


def make_deps(store, planner, validator, composer=None, tmp_path=None, max_attempts=3) -> Deps:
    settings = Settings(
        storage=tmp_path or Path("storage"), max_attempts=max_attempts, model="fake:test"
    )
    return Deps(
        planner=planner,
        composer=composer or ScriptedComposer(),
        validator=validator,
        open_document=store.opener(),
        settings=settings,
    )


def run(deps, *, thread: str = "t1"):
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": thread}}
    result = graph.invoke(initial_state(SOURCE, "Make the introduction concise."), config)
    return graph, config, result


def test_happy_path_pauses_for_approval_then_finalizes(store, tmp_path):
    deps = make_deps(
        store,
        ScriptedPlanner([[edit("s1", "b4")]]),
        ScriptedValidator([passing()]),
        tmp_path=tmp_path,
    )
    graph, config, paused = run(deps)

    assert "__interrupt__" in paused
    assert paused["status"] == "awaiting_approval"
    assert "- Throughout the quarter" in paused["diff"]
    assert "+ A concise rewrite." in paused["diff"]
    assert paused["output_path"] is None

    final = graph.invoke(Command(resume={"approved": True}), config)

    assert final["status"] == "finalized"
    assert final["approved"] is True
    assert final["output_path"].endswith("job.docx")
    assert final["output_path"] in store.saved


def test_composer_receives_the_full_block_text(store, tmp_path):
    composer = ScriptedComposer()
    deps = make_deps(
        store,
        ScriptedPlanner([[edit("s1", "b4", "Trim it.")]]),
        ScriptedValidator([passing()]),
        composer=composer,
        tmp_path=tmp_path,
    )
    run(deps)

    assert composer.calls == [
        {
            "instruction": "Trim it.",
            "current_text": "Throughout the quarter the team maintained a steady cadence.",
        }
    ]


def test_preview_failure_routes_back_to_the_planner(store, tmp_path):
    planner = ScriptedPlanner([[edit("s1", "nonexistent")], [edit("s1", "b4")]])
    deps = make_deps(store, planner, ScriptedValidator([passing()]), tmp_path=tmp_path)
    _, _, paused = run(deps)

    assert len(planner.calls) == 2
    assert planner.calls[0] is None
    assert "TARGET_NOT_FOUND" in planner.calls[1]
    assert paused["status"] == "awaiting_approval"
    assert paused["attempts"] == 2


def test_semantic_failure_routes_back_to_the_planner(store, tmp_path):
    planner = ScriptedPlanner([[edit("s1", "b4")]])
    validator = ScriptedValidator([failing("barely shorter"), passing()])
    deps = make_deps(store, planner, validator, tmp_path=tmp_path)
    _, _, paused = run(deps)

    assert validator.calls == 2
    assert "barely shorter" in planner.calls[1]
    assert paused["status"] == "awaiting_approval"


def test_retry_cap_stops_a_runaway_loop(store, tmp_path):
    planner = ScriptedPlanner([[edit("s1", "b4")]])
    validator = ScriptedValidator([failing()])
    deps = make_deps(store, planner, validator, tmp_path=tmp_path, max_attempts=3)
    _, _, result = run(deps)

    assert result["status"] == "failed"
    assert result["attempts"] == 3
    assert (
        planner.calls
        == [None] + ["the applied edits did not satisfy the request: still too long"] * 2
    )
    assert "gave up after 3 attempt(s)" in result["errors"][-1]


def test_unresolvable_target_eventually_fails(store, tmp_path):
    planner = ScriptedPlanner([[edit("s1", "ghost")]])
    deps = make_deps(store, planner, ScriptedValidator([passing()]), tmp_path=tmp_path)
    _, _, result = run(deps)

    assert result["status"] == "failed"
    assert result["preview_failures"][0]["code"] == "TARGET_NOT_FOUND"


def test_rejection_replans_with_the_reviewers_feedback(store, tmp_path):
    planner = ScriptedPlanner([[edit("s1", "b4")]])
    deps = make_deps(
        store,
        planner,
        ScriptedValidator([passing()]),
        composer=ScriptedComposer("A second attempt."),
        tmp_path=tmp_path,
    )
    graph, config, paused = run(deps)
    assert paused["attempts"] == 1

    again = graph.invoke(
        Command(resume={"approved": False, "feedback": "keep the second sentence"}), config
    )

    assert again["status"] == "awaiting_approval"
    assert planner.calls[1] == "keep the second sentence"
    # a human rejection is not a runaway loop, so the retry budget starts over
    assert again["attempts"] == 1
    assert "+ A second attempt." in again["diff"]


def test_rejection_does_not_consume_the_retry_budget(store, tmp_path):
    """Three rejections in a row must not trip the cap that guards automated retries."""
    deps = make_deps(
        store,
        ScriptedPlanner([[edit("s1", "b4")]]),
        ScriptedValidator([passing()]),
        tmp_path=tmp_path,
        max_attempts=3,
    )
    graph, config, state = run(deps)

    for _ in range(3):
        state = graph.invoke(Command(resume={"approved": False, "feedback": "again"}), config)
        assert state["status"] == "awaiting_approval"

    final = graph.invoke(Command(resume={"approved": True}), config)
    assert final["status"] == "finalized"


def test_edits_always_reapply_from_the_untouched_source(store, tmp_path):
    """Each attempt re-applies from the original, so a failed proposal needs no rollback."""
    planner = ScriptedPlanner([[edit("s1", "b4")]])
    deps = make_deps(store, planner, ScriptedValidator([failing(), passing()]), tmp_path=tmp_path)
    run(deps)

    source = str(Path(SOURCE))
    assert store.opened.count(source) >= 4  # analyze + (preview, apply) per attempt
    assert store.docs[source].changes == []  # the source itself was never mutated


def test_approval_state_survives_a_rebuilt_graph(store, tmp_path):
    """The pause outlives the process: a fresh graph object resumes from the checkpoint."""
    checkpointer = InMemorySaver()
    deps = make_deps(
        store,
        ScriptedPlanner([[edit("s1", "b4")]]),
        ScriptedValidator([passing()]),
        tmp_path=tmp_path,
    )
    config = {"configurable": {"thread_id": "survives"}}

    first = build_graph(deps, checkpointer=checkpointer)
    paused = first.invoke(initial_state(SOURCE, "Make it concise."), config)
    assert "__interrupt__" in paused

    del first
    second = build_graph(deps, checkpointer=checkpointer)
    final = second.invoke(Command(resume={"approved": True}), config)

    assert final["status"] == "finalized"
    assert final["output_path"] in store.saved


def test_hallucinated_block_is_retried_never_partially_applied(store, tmp_path):
    """The plan is atomic: one bad target rejects the batch rather than applying the rest."""
    planner = ScriptedPlanner([[edit("s1", "b4"), edit("s2", "made-up")], [edit("s1", "b4")]])
    deps = make_deps(store, planner, ScriptedValidator([passing()]), tmp_path=tmp_path)
    _, _, paused = run(deps)

    assert "made-up" in planner.calls[1]
    assert len(paused["changes"]) == 1
    assert paused["status"] == "awaiting_approval"
    assert store.saved  # nothing was written until a whole plan previewed clean


def test_empty_document_fails_before_planning(tmp_path):
    store = FakeDocumentStore()
    store.seed(Path(SOURCE), blocks=[])
    planner = ScriptedPlanner([[edit("s1", "b4")]])
    deps = make_deps(store, planner, ScriptedValidator([passing()]), tmp_path=tmp_path)
    _, _, result = run(deps)

    assert result["status"] == "failed"
    assert planner.calls == []
