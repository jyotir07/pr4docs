"""The whole loop against a real model and a real .docx.

Marked `live`: costs money and needs OPENAI_API_KEY. Run with `pytest -m live`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from pr4docs.config import Settings
from pr4docs.graph import build_graph
from pr4docs.roles import build_deps
from pr4docs.state import initial_state

pytestmark = pytest.mark.live

REQUEST = "Make the Introduction section about 40% shorter while keeping the key points."


def paragraph_texts(path: Path) -> list[str]:
    return [p.text for p in DocxDocument(str(path)).paragraphs if p.text.strip()]


def test_full_loop_against_a_real_model(sample_docx: Path, tmp_path: Path):
    deps = replace(build_deps(), settings=Settings(storage=tmp_path, max_attempts=3))
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "live-1"}}

    paused = graph.invoke(initial_state(str(sample_docx), REQUEST), config)

    assert "__interrupt__" in paused, paused.get("errors")
    assert paused["status"] == "awaiting_approval"
    assert paused["changes"], "the model produced no changes"

    # the planner must have targeted the Introduction, not the Infrastructure section
    edited = {c["block_id"] for c in paused["changes"]}
    assert "00000006" not in edited

    for change in paused["changes"]:
        assert len(change["after"]) < len(change["before"]), "the rewrite got longer"

    original = paragraph_texts(sample_docx)
    final = graph.invoke(Command(resume={"approved": True}), config)

    assert final["status"] == "finalized"
    out = Path(final["output_path"])
    assert out.exists()

    produced = paragraph_texts(out)
    assert len(produced) == len(original), "paragraph count changed"
    assert produced[0] == original[0], "the title was not supposed to change"
    assert produced[-1] == original[-1], "the Infrastructure paragraph was not supposed to change"
    assert produced != original


def test_rejection_produces_a_different_proposal(sample_docx: Path, tmp_path: Path):
    deps = replace(build_deps(), settings=Settings(storage=tmp_path, max_attempts=3))
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "live-2"}}

    first = graph.invoke(initial_state(str(sample_docx), REQUEST), config)
    first_diff = first["diff"]

    second = graph.invoke(
        Command(
            resume={
                "approved": False,
                "feedback": "Too aggressive. Only shorten the second paragraph, "
                "and leave the first one exactly as it is.",
            }
        ),
        config,
    )

    assert second["status"] == "awaiting_approval"
    assert second["diff"] != first_diff
    assert len(second["changes"]) == 1
