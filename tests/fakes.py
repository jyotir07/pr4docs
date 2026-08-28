"""In-memory stand-ins for the document layer and the three LLM roles.

The fake session mirrors the real one's contract as pinned by test_superdoc_contract:
preview reports TARGET_NOT_FOUND for unknown blocks, apply produces one tracked change
per step with before/after text, and accepting resolves them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from pr4docs.deps import PlannedEdit, Validation
from pr4docs.docs.superdoc import (
    Block,
    Change,
    DocumentError,
    PlanFailure,
    PreviewResult,
)

DEFAULT_BLOCKS = [
    Block("b1", "heading", 0, "Quarterly Engineering Report", heading_level=1),
    Block("b2", "heading", 1, "Introduction", heading_level=2),
    Block("b3", "paragraph", 2, "This document summarizes the engineering work completed."),
    Block("b4", "paragraph", 3, "Throughout the quarter the team maintained a steady cadence."),
]


@dataclass
class FakeDoc:
    blocks: list[Block]
    changes: list[Change] = field(default_factory=list)


@dataclass
class FakeDocumentStore:
    """Stands in for the filesystem: paths map to documents, and saving copies one."""

    docs: dict[str, FakeDoc] = field(default_factory=dict)
    opened: list[str] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    autoseed: bool = False
    """For API tests, where the upload path is a uuid this store cannot know in advance."""

    def seed(self, path: Path, blocks: list[Block] | None = None) -> None:
        chosen = DEFAULT_BLOCKS if blocks is None else blocks
        self.docs[str(path)] = FakeDoc(blocks=list(chosen))

    def opener(self):
        @contextmanager
        def _open(path: Path) -> Iterator[FakeSession]:
            key = str(path)
            if key not in self.docs:
                if not self.autoseed:
                    raise DocumentError(f"no such document: {key}")
                self.seed(path)
            self.opened.append(key)
            # the real session closes with discard=True, so edits reach disk only via
            # save(); the session works on a copy to model that
            stored = self.docs[key]
            yield FakeSession(self, FakeDoc(list(stored.blocks), list(stored.changes)))

        return _open


class FakeSession:
    def __init__(self, store: FakeDocumentStore, doc: FakeDoc) -> None:
        self._store = store
        self._doc = doc

    def outline(self, limit: int = 500) -> list[Block]:
        return list(self._doc.blocks)

    def text(self) -> str:
        return "\n".join(b.text for b in self._doc.blocks)

    def _resolve(self, steps) -> list[PlanFailure]:
        known = {b.node_id for b in self._doc.blocks}
        return [
            PlanFailure(
                step_id=s.step_id,
                code="TARGET_NOT_FOUND",
                phase="compile",
                message=f'ref "{s.node_id}" did not resolve to a block in the scoped story.',
            )
            for s in steps
            if s.node_id not in known
        ]

    def preview(self, steps, *, change_mode="tracked", expected_revision=None) -> PreviewResult:
        failures = self._resolve(steps)
        if failures:
            return PreviewResult(False, failures, {})
        by_id = {b.node_id: b for b in self._doc.blocks}
        return PreviewResult(True, [], {s.step_id: by_id[s.node_id].text for s in steps})

    def apply(self, steps, *, change_mode="tracked", expected_revision=None) -> dict:
        failures = self._resolve(steps)
        if failures:
            raise DocumentError(f"apply failed: {failures[0].message}")

        by_id = {b.node_id: b for b in self._doc.blocks}
        for s in steps:
            before = by_id[s.node_id].text
            self._doc.changes.append(
                Change(
                    change_id=f"tc-{s.step_id}",
                    change_type="replacement",
                    before=before,
                    after=s.text,
                    block_id=s.node_id,
                    author="fake",
                )
            )
        return {"success": True, "revision": {"before": "0", "after": "1"}}

    def changes(self, limit: int = 200) -> list[Change]:
        return list(self._doc.changes)

    def decide_all(self, decision: str) -> dict:
        if decision == "accept":
            blocks = {b.node_id: b for b in self._doc.blocks}
            for c in self._doc.changes:
                if c.block_id in blocks:
                    blocks[c.block_id] = replace(blocks[c.block_id], text=c.after)
            self._doc.blocks = [blocks[b.node_id] for b in self._doc.blocks]
        self._doc.changes = []
        return {"success": True}

    def save(self, out: Path) -> Path:
        self._store.saved.append(str(out))
        self._store.docs[str(out)] = FakeDoc(
            blocks=list(self._doc.blocks), changes=list(self._doc.changes)
        )
        return out


# --- the three LLM roles -----------------------------------------------------


@dataclass
class ScriptedPlanner:
    """Returns the next scripted batch per call, so retries can differ from the first try."""

    batches: list[list[PlannedEdit]]
    calls: list[str | None] = field(default_factory=list)

    def __call__(self, *, request, outline, feedback):
        self.calls.append(feedback)
        index = min(len(self.calls) - 1, len(self.batches) - 1)
        return self.batches[index]


@dataclass
class ScriptedComposer:
    text: str = "A concise rewrite."
    calls: list[dict] = field(default_factory=list)

    def __call__(self, *, request, instruction, current_text):
        self.calls.append({"instruction": instruction, "current_text": current_text})
        return self.text


@dataclass
class ScriptedValidator:
    verdicts: list[Validation]
    calls: int = 0

    def __call__(self, *, request, changes):
        index = min(self.calls, len(self.verdicts) - 1)
        self.calls += 1
        return self.verdicts[index]


def edit(step_id: str, node_id: str, instruction: str = "Make it concise.") -> PlannedEdit:
    return PlannedEdit(
        step_id=step_id, node_id=node_id, node_type="paragraph", instruction=instruction
    )


def passing() -> Validation:
    return Validation(True, "satisfies the request")


def failing(reason: str = "still too long") -> Validation:
    return Validation(False, reason)
