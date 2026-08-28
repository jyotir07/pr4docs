from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from langgraph.types import interrupt

from pr4docs.deps import Deps, PlannedEdit
from pr4docs.docs.superdoc import Block, Change, DocumentError, EditStep, PlanFailure
from pr4docs.state import PR4DocsState


class Node(Protocol):
    """Matches langgraph's node protocol, which requires the parameter be named `state`."""

    def __call__(self, state: PR4DocsState) -> dict[str, Any]: ...


def _blocks(state: PR4DocsState) -> list[Block]:
    return [Block(**b) for b in state.get("outline", [])]


def _steps(state: PR4DocsState) -> list[EditStep]:
    return [EditStep(**s) for s in state.get("steps", [])]


def _working_path(deps: Deps, state: PR4DocsState) -> Path:
    stem = Path(state["source_path"]).stem
    return deps.settings.working / f"{stem}.a{state.get('attempts', 0)}.docx"


def summarize_failures(failures: list[PlanFailure]) -> str:
    return "The previous edit plan was rejected before it was applied:\n" + "\n".join(
        f"- {f.describe()}" for f in failures
    )


def render_diff(changes: list[Change]) -> str:
    if not changes:
        return "(no changes)"
    parts = []
    for c in changes:
        parts.append(f"@@ block {c.block_id or '?'} @@")
        if c.before:
            parts.append(f"- {c.before}")
        if c.after:
            parts.append(f"+ {c.after}")
    return "\n".join(parts)


def make_analyze(deps: Deps) -> Node:
    def analyze(state: PR4DocsState) -> dict[str, Any]:
        with deps.open_document(Path(state["source_path"])) as session:
            outline = session.outline()
        if not outline:
            return {"errors": ["document has no addressable blocks"], "outline": []}
        return {"outline": [asdict(b) for b in outline], "status": "planning"}

    return analyze


def make_plan(deps: Deps) -> Node:
    def plan(state: PR4DocsState) -> dict[str, Any]:
        edits = deps.planner(
            request=state["request"],
            outline=_blocks(state),
            feedback=state.get("revise_feedback"),
        )
        # Deliberately not filtering out unknown node_ids here. Preview is free, local,
        # and atomic, so it is the one place that judges a plan — and routing a
        # hallucinated block back through it is exactly what the retry loop is for.
        # revise_feedback is deliberately NOT cleared here: compose runs after this node
        # and needs to know why the last attempt was rejected. validate clears it on success.
        return {
            "plan": [asdict(e) for e in edits],
            "attempts": state.get("attempts", 0) + 1,
            "preview_failures": [],
        }

    return plan


def make_compose(deps: Deps) -> Node:
    def compose(state: PR4DocsState) -> dict[str, Any]:
        by_id = {b.node_id: b for b in _blocks(state)}
        # steps still hold the rejected attempt's text at this point, which is what lets
        # the composer see what it wrote last time rather than starting from scratch
        previous = {s["node_id"]: s["text"] for s in state.get("steps", [])}
        feedback = state.get("revise_feedback")

        steps = []
        for raw in state.get("plan", []):
            edit = PlannedEdit(**raw)
            block = by_id.get(edit.node_id)
            text = deps.composer(
                request=state["request"],
                instruction=edit.instruction,
                current_text=block.text if block else "",
                feedback=feedback,
                previous_text=previous.get(edit.node_id),
            )
            steps.append(
                asdict(
                    EditStep(
                        step_id=edit.step_id,
                        node_id=edit.node_id,
                        node_type=edit.node_type,
                        op=edit.op,
                        text=text,
                    )
                )
            )
        return {"steps": steps}

    return compose


def make_preview(deps: Deps) -> Node:
    """Structural validation: free, deterministic, and it never touches the document."""

    def preview(state: PR4DocsState) -> dict[str, Any]:
        with deps.open_document(Path(state["source_path"])) as session:
            result = session.preview(_steps(state))
        if result.valid:
            return {"preview_failures": []}
        return {
            "preview_failures": [asdict(f) for f in result.failures],
            "revise_feedback": summarize_failures(result.failures),
        }

    return preview


def make_apply(deps: Deps) -> Node:
    def apply(state: PR4DocsState) -> dict[str, Any]:
        working = _working_path(deps, state)
        try:
            with deps.open_document(Path(state["source_path"])) as session:
                session.apply(_steps(state))
                session.save(working)
                changes = session.changes()
        except DocumentError as exc:
            return {
                "errors": [str(exc)],
                "revise_feedback": f"applying the edits failed: {exc}",
                "changes": [],
            }
        return {"working_path": str(working), "changes": [asdict(c) for c in changes]}

    return apply


def make_validate(deps: Deps) -> Node:
    """Semantic check only — structure was already proven by preview."""

    def validate(state: PR4DocsState) -> dict[str, Any]:
        changes = [Change(**c) for c in state.get("changes", [])]
        verdict = deps.validator(request=state["request"], changes=changes)
        if verdict.passed:
            return {"validation": asdict(verdict), "revise_feedback": None}
        return {
            "validation": asdict(verdict),
            "revise_feedback": f"the applied edits did not satisfy the request: {verdict.reason}",
        }

    return validate


def make_diff(deps: Deps) -> Node:
    def diff(state: PR4DocsState) -> dict[str, Any]:
        changes = [Change(**c) for c in state.get("changes", [])]
        return {"diff": render_diff(changes), "status": "awaiting_approval"}

    return diff


def make_approval(deps: Deps) -> Node:
    """Pauses the graph. On resume LangGraph re-runs this node from the top, so the
    interrupt must be the first thing it does and nothing may happen before it."""

    def approval(state: PR4DocsState) -> dict[str, Any]:
        decision = interrupt({"diff": state.get("diff", ""), "changes": state.get("changes", [])})

        if isinstance(decision, bool):
            approved, feedback = decision, None
        else:
            approved = bool(decision.get("approved"))
            feedback = decision.get("feedback")

        if approved:
            return {"approved": True}
        return {
            "approved": False,
            # a person clicking reject is not a runaway loop, so the retry budget resets
            "attempts": 0,
            "revise_feedback": feedback or "the reviewer rejected the changes without a reason",
        }

    return approval


def make_finalize(deps: Deps) -> Node:
    def finalize(state: PR4DocsState) -> dict[str, Any]:
        working = state.get("working_path")
        if not working:
            return {"status": "failed", "errors": ["nothing to finalize"]}

        out = deps.settings.output / f"{Path(state['source_path']).stem}.docx"
        with deps.open_document(Path(working)) as session:
            session.decide_all("accept")
            session.save(out)
        return {"output_path": str(out), "status": "finalized"}

    return finalize


def make_fail(deps: Deps) -> Node:
    def fail(state: PR4DocsState) -> dict[str, Any]:
        reason = state.get("revise_feedback") or "the graph could not produce a valid edit"
        return {
            "status": "failed",
            "errors": [f"gave up after {state.get('attempts', 0)} attempt(s): {reason}"],
        }

    return fail
