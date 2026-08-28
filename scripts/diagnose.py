"""Instrument every boundary in one real run: outline -> planner -> composer -> validator.

    uv run python scripts/diagnose.py

Hits the real model, so it costs money. This is what found the two convergence bugs:
the planner emitting byte-identical instructions across retries, and the validator
judging length by eye. Wraps the roles rather than editing them, so what runs here is
exactly what runs in production.
"""

import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

import tempfile  # noqa: E402

from conftest import write_sample_docx  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from pr4docs.config import Settings  # noqa: E402
from pr4docs.graph import build_graph  # noqa: E402
from pr4docs.roles import build_deps  # noqa: E402
from pr4docs.state import initial_state  # noqa: E402

REQUEST = "Make the Introduction section about 40% shorter while keeping the key points."


def bar(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


class LoggingPlanner:
    def __init__(self, inner):
        self.inner = inner

    def __call__(self, *, request, outline, feedback):
        bar(f"PLANNER  (feedback={feedback!r})")
        print("outline the planner sees:")
        for b in outline:
            print(f"   [{b.node_id}] {b.node_type:9} len={len(b.text):4}  {b.text[:70]}")
        edits = self.inner(request=request, outline=outline, feedback=feedback)
        print("\nplanner emitted:")
        for e in edits:
            print(f"   {e.step_id} -> {e.node_id} ({e.op})")
            print(f"      instruction: {e.instruction}")
        return edits


class LoggingComposer:
    def __init__(self, inner):
        self.inner = inner

    def __call__(self, *, request, instruction, current_text, feedback=None, previous_text=None):
        out = self.inner(
            request=request,
            instruction=instruction,
            current_text=current_text,
            feedback=feedback,
            previous_text=previous_text,
        )
        bar("COMPOSER")
        print(f"instruction : {instruction}")
        print(f"feedback    : {feedback}")
        print(f"prev attempt: {previous_text}")
        print(f"IN  ({len(current_text):4} chars): {current_text}")
        print(f"OUT ({len(out):4} chars): {out}")
        delta = (1 - len(out) / len(current_text)) * 100 if current_text else 0
        print(f">>> reduction: {delta:.1f}%")
        return out


class LoggingValidator:
    def __init__(self, inner):
        self.inner = inner

    def __call__(self, *, request, changes):
        verdict = self.inner(request=request, changes=changes)
        bar("VALIDATOR")
        for c in changes:
            d = (1 - len(c.after) / len(c.before)) * 100 if c.before else 0
            print(
                f"   block {c.block_id}: {len(c.before)} -> {len(c.after)} chars ({d:.1f}% shorter)"
            )
        print(f"\n   passed: {verdict.passed}\n   reason: {verdict.reason}")
        return verdict


def main():
    tmp = Path(tempfile.mkdtemp())
    docx = write_sample_docx(tmp / "sample.docx")

    base = build_deps()
    deps = replace(
        base,
        planner=LoggingPlanner(base.planner),
        composer=LoggingComposer(base.composer),
        validator=LoggingValidator(base.validator),
        settings=Settings(storage=tmp, max_attempts=3),
    )

    graph = build_graph(deps, checkpointer=InMemorySaver())
    result = graph.invoke(
        initial_state(str(docx), REQUEST), {"configurable": {"thread_id": "diag"}}
    )

    bar("OUTCOME")
    print("status  :", result.get("status"))
    print("attempts:", result.get("attempts"))
    for e in result.get("errors", []):
        print("error   :", e)


if __name__ == "__main__":
    main()
