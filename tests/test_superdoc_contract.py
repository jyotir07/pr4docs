"""Pins the SuperDoc SDK behaviour the graph depends on.

Marked `contract`: each test spawns the embedded editor process, so these are excluded
from the default run. Use `pytest -m contract` to check the SDK still behaves.
"""

from pathlib import Path

import pytest

from pr4docs.docs.superdoc import DocumentError, EditStep, open_document

pytestmark = pytest.mark.contract

CONCISE = "The team held a steady release cadence while absorbing a large migration."


def body_paragraphs(session):
    return [b for b in session.outline() if b.node_type == "paragraph"]


def test_outline_exposes_stable_block_addresses(sample_docx: Path):
    with open_document(sample_docx) as session:
        blocks = session.outline()

    assert [b.node_type for b in blocks[:3]] == ["heading", "heading", "paragraph"]
    assert blocks[1].text == "Introduction"
    assert blocks[1].heading_level == 2
    assert all(b.node_id for b in blocks)
    assert len({b.node_id for b in blocks}) == len(blocks)


def test_preview_validates_without_mutating(sample_docx: Path):
    with open_document(sample_docx) as session:
        target = body_paragraphs(session)[1]
        before = session.text()

        result = session.preview([EditStep("s1", target.node_id, target.node_type, text=CONCISE)])

        assert result.valid
        assert result.failures == []
        # preview hands back the text currently at the target, which the composer needs
        assert "steady release cadence" in result.resolved_text["s1"]
        assert session.text() == before


def test_preview_reports_unresolvable_target(sample_docx: Path):
    with open_document(sample_docx) as session:
        result = session.preview([EditStep("s1", "99999999", "paragraph", text="nope")])

    assert not result.valid
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.code == "TARGET_NOT_FOUND"
    assert failure.step_id == "s1"
    assert failure.phase == "compile"


def test_preview_normalizes_a_raised_sdk_error(sample_docx: Path):
    """A malformed plan raises instead of returning valid=false; both must look alike."""
    with open_document(sample_docx) as session:
        target = body_paragraphs(session)[0]
        bogus = EditStep("s1", target.node_id, target.node_type, op="text.nonsense", text="x")  # type: ignore[arg-type]

        result = session.preview([bogus])

    assert not result.valid
    assert result.failures[0].code == "PLAN_REJECTED"
    assert "text.nonsense" in result.failures[0].message


def test_apply_tracked_produces_a_reviewable_change(sample_docx: Path):
    with open_document(sample_docx) as session:
        target = body_paragraphs(session)[1]
        original = target.text

        receipt = session.apply([EditStep("s1", target.node_id, target.node_type, text=CONCISE)])
        assert receipt["revision"]["before"] != receipt["revision"]["after"]

        changes = session.changes()

    assert len(changes) == 1
    change = changes[0]
    assert change.after == CONCISE
    assert change.before.startswith(original[:40])
    assert change.block_id == target.node_id


def test_apply_rejects_an_invalid_plan(sample_docx: Path):
    with open_document(sample_docx) as session, pytest.raises(DocumentError):
        session.apply([EditStep("s1", "99999999", "paragraph", text="nope")])


def test_tracked_changes_survive_a_process_boundary(sample_docx: Path, tmp_path: Path):
    """The approval pause outlives the session, so the redline has to live in the file."""
    working = tmp_path / "working.docx"

    with open_document(sample_docx) as session:
        target = body_paragraphs(session)[1]
        session.apply([EditStep("s1", target.node_id, target.node_type, text=CONCISE)])
        session.save(working)

    # a completely separate client/process, as after a server restart
    with open_document(working) as reopened:
        assert len(reopened.changes()) == 1

        final = tmp_path / "final.docx"
        reopened.decide_all("accept")
        reopened.save(final)

    with open_document(final) as done:
        assert done.changes() == []
        assert CONCISE in done.text()
        assert "not without cost" not in done.text()


def test_reject_restores_the_original_text(sample_docx: Path, tmp_path: Path):
    with open_document(sample_docx) as session:
        target = body_paragraphs(session)[1]
        before = session.text()

        session.apply([EditStep("s1", target.node_id, target.node_type, text=CONCISE)])
        session.decide_all("reject")

        assert session.text() == before
        assert session.changes() == []
