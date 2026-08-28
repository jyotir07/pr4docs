"""Instrument every boundary in one real run: outline -> planner -> composer -> validator.

    uv run python scripts/diagnose.py

Hits the real model, so it costs money. This is what found the two convergence bugs:
the planner emitting byte-identical instructions across retries, and the validator
judging length by eye. Wraps the roles rather than editing them, so what runs here is
exactly what runs in production.
"""

import sys
from dataclasses import dataclass, field, replace
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


@dataclass
class Attempt:
    n: int
    blocks: int = 0
    saw_feedback: bool = False
    before: int = 0
    after: int = 0
    passed: bool | None = None
    reason: str = ""


@dataclass
class Recorder:
    """One row per attempt. `saw_feedback` is the variable the whole retry design turns
    on, so it sits next to the reduction it is supposed to move."""

    attempts: list[Attempt] = field(default_factory=list)

    def start(self):
        self.attempts.append(Attempt(n=len(self.attempts) + 1))

    @property
    def current(self):
        return self.attempts[-1]


class LoggingPlanner:
    def __init__(self, inner, rec):
        self.inner = inner
        self.rec = rec

    def __call__(self, *, request, outline, feedback):
        self.rec.start()
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
    def __init__(self, inner, rec):
        self.inner = inner
        self.rec = rec

    def __call__(self, *, request, instruction, current_text, feedback=None, previous_text=None):
        self.rec.current.blocks += 1
        self.rec.current.saw_feedback = feedback is not None
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
    def __init__(self, inner, rec):
        self.inner = inner
        self.rec = rec

    def __call__(self, *, request, changes):
        verdict = self.inner(request=request, changes=changes)
        # measured off what actually landed in the document, not what the composer returned
        self.rec.current.before = sum(len(c.before) for c in changes)
        self.rec.current.after = sum(len(c.after) for c in changes)
        self.rec.current.passed = verdict.passed
        self.rec.current.reason = verdict.reason
        bar("VALIDATOR")
        for c in changes:
            d = (1 - len(c.after) / len(c.before)) * 100 if c.before else 0
            print(
                f"   block {c.block_id}: {len(c.before)} -> {len(c.after)} chars ({d:.1f}% shorter)"
            )
        print(f"\n   passed: {verdict.passed}\n   reason: {verdict.reason}")
        return verdict


ROW = "  {:>7}  {:>6}  {:>8}  {:>15}  {:>7}  {}"


def print_summary(rec, result):
    bar("SUMMARY")
    print(ROW.format("attempt", "blocks", "feedback", "before -> after", "overall", "verdict"))
    print("  " + "-" * 76)

    for a in rec.attempts:
        if a.passed is None:
            verdict = "no verdict (rejected before validation)"
        elif a.passed:
            verdict = "pass"
        else:
            verdict = f"reject: {a.reason}"
        # the full reason is already in the VALIDATOR block above; keep the row narrow
        # enough to screenshot
        if len(verdict) > 40:
            verdict = verdict[:39].rsplit(" ", 1)[0] + "..."
        print(
            ROW.format(
                a.n,
                a.blocks,
                "yes" if a.saw_feedback else "no",
                f"{a.before} -> {a.after}" if a.before else "-",
                f"{(1 - a.after / a.before) * 100:.1f}%" if a.before else "-",
                verdict[:40],
            )
        )

    print(f"\n  outcome: {result.get('status')} after {result.get('attempts')} attempt(s)")
    for e in result.get("errors", []):
        print(f"  error  : {e}")


def main():
    tmp = Path(tempfile.mkdtemp())
    docx = write_sample_docx(tmp / "sample.docx")

    rec = Recorder()
    base = build_deps()
    deps = replace(
        base,
        planner=LoggingPlanner(base.planner, rec),
        composer=LoggingComposer(base.composer, rec),
        validator=LoggingValidator(base.validator, rec),
        settings=Settings(storage=tmp, max_attempts=3),
    )

    graph = build_graph(deps, checkpointer=InMemorySaver())
    result = graph.invoke(
        initial_state(str(docx), REQUEST), {"configurable": {"thread_id": "diag"}}
    )

    print_summary(rec, result)


if __name__ == "__main__":
    main()
