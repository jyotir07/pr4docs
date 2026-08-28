"""The only module that touches the SuperDoc SDK.

Everything the graph needs from a document goes through `DocumentSession`, so the
nodes stay testable against a fake and the SDK's shapes stay in one place.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from superdoc import SuperDocClient, SuperDocError

# Confirmed against superdoc-sdk 2.7.0 via doc.capabilities.get().planEngine:
# text.rewrite, text.delete, text.insert, format.apply, assert.
# The MVP deliberately uses only the first two — a paragraph rewrite expresses
# "add a sentence" just as well, without insert's anchor-positioning semantics.
StepOp = Literal["text.rewrite", "text.delete"]

ChangeMode = Literal["tracked", "direct"]
Decision = Literal["accept", "reject"]


@dataclass(frozen=True)
class Block:
    """One addressable node in the document. `node_id` is stable across revisions."""

    node_id: str
    node_type: str
    ordinal: int
    text: str
    heading_level: int | None = None
    style_id: str | None = None

    @property
    def is_heading(self) -> bool:
        return self.node_type == "heading"


@dataclass(frozen=True)
class EditStep:
    step_id: str
    node_id: str
    node_type: str
    op: StepOp = "text.rewrite"
    text: str = ""


@dataclass(frozen=True)
class PlanFailure:
    """A rejected step, normalized from either failure path (see `_normalize_error`)."""

    step_id: str
    code: str
    phase: str
    message: str

    def describe(self) -> str:
        return f"step {self.step_id}: {self.code} ({self.phase}) — {self.message}"


@dataclass(frozen=True)
class PreviewResult:
    valid: bool
    failures: list[PlanFailure]
    resolved_text: dict[str, str]
    """step_id -> the text currently at that target, straight from the preview."""


@dataclass(frozen=True)
class Change:
    change_id: str
    change_type: str
    before: str
    after: str
    block_id: str | None
    author: str | None


class DocumentError(RuntimeError):
    """A document operation failed in a way the graph cannot plan around."""


def _blocks_from(payload: dict[str, Any]) -> list[Block]:
    return [
        Block(
            node_id=str(b["nodeId"]),
            node_type=str(b["nodeType"]),
            ordinal=int(b.get("ordinal", i)),
            text=str(b.get("text", b.get("textPreview", ""))),
            heading_level=b.get("headingLevel"),
            style_id=b.get("styleId"),
        )
        for i, b in enumerate(payload.get("blocks") or [])
    ]


def _normalize_error(exc: SuperDocError, steps: Sequence[EditStep]) -> list[PlanFailure]:
    """A malformed plan raises; a resolvable-but-failing plan returns valid=false.

    Both have to reach the planner as the same shape, otherwise the retry loop only
    self-corrects for half its failure modes.
    """
    step_id = steps[0].step_id if len(steps) == 1 else "*"
    return [
        PlanFailure(
            step_id=step_id,
            code="PLAN_REJECTED",
            phase="request",
            message=str(exc),
        )
    ]


class DocumentSession:
    """A single open document. Not thread-safe: it owns one editor subprocess."""

    def __init__(self, handle: Any) -> None:
        self._doc = handle

    # --- reading -------------------------------------------------------------

    def outline(self, limit: int = 500) -> list[Block]:
        """Full block text, not the truncated preview — the composer rewrites from it."""
        return _blocks_from(self._doc.blocks.list({"limit": limit, "includeText": True}))

    def text(self) -> str:
        return str(self._doc.get_text())

    def revision(self) -> str | None:
        rev = self._doc.blocks.list({"limit": 1}).get("revision")
        return str(rev) if rev is not None else None

    # --- editing -------------------------------------------------------------

    def _plan(
        self,
        steps: Sequence[EditStep],
        change_mode: ChangeMode,
        expected_revision: str | None,
    ) -> dict[str, Any]:
        plan: dict[str, Any] = {
            "atomic": True,
            "changeMode": change_mode,
            "steps": [
                {
                    "id": s.step_id,
                    "op": s.op,
                    "where": {"by": "block", "nodeType": s.node_type, "nodeId": s.node_id},
                    "args": {"replacement": {"text": s.text}},
                }
                for s in steps
            ],
        }
        if expected_revision is not None:
            plan["expectedRevision"] = expected_revision
        return plan

    def preview(
        self,
        steps: Sequence[EditStep],
        *,
        change_mode: ChangeMode = "tracked",
        expected_revision: str | None = None,
    ) -> PreviewResult:
        """Validate every step without touching the document. Free and deterministic."""
        try:
            raw = self._doc.mutations.preview(self._plan(steps, change_mode, expected_revision))
        except SuperDocError as exc:
            return PreviewResult(False, _normalize_error(exc, steps), {})

        failures = [
            PlanFailure(
                step_id=str(f.get("stepId", "?")),
                code=str(f.get("code", "UNKNOWN")),
                phase=str(f.get("phase", "unknown")),
                message=str(f.get("message", "")),
            )
            for f in raw.get("failures") or []
        ]
        resolved = {
            str(s.get("stepId")): str((s.get("resolutions") or [{}])[0].get("text", ""))
            for s in raw.get("steps") or []
        }
        return PreviewResult(bool(raw.get("valid")), failures, resolved)

    def apply(
        self,
        steps: Sequence[EditStep],
        *,
        change_mode: ChangeMode = "tracked",
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        try:
            receipt: dict[str, Any] = self._doc.mutations.apply(
                self._plan(steps, change_mode, expected_revision)
            )
        except SuperDocError as exc:
            raise DocumentError(f"apply failed: {exc}") from exc

        if not receipt.get("success"):
            raise DocumentError(f"apply returned success=false: {receipt}")
        return receipt

    # --- review --------------------------------------------------------------

    def changes(self, limit: int = 200) -> list[Change]:
        raw = self._doc.track_changes.list({"limit": limit})
        return [
            Change(
                change_id=str(c.get("id")),
                change_type=str(c.get("type", "")),
                before=str(c.get("deletedText") or ""),
                after=str(c.get("insertedText") or ""),
                block_id=(c.get("navigationTarget") or {}).get("blockId"),
                author=c.get("author"),
            )
            for c in raw.get("items") or []
        ]

    def decide_all(self, decision: Decision) -> dict[str, Any]:
        try:
            receipt: dict[str, Any] = self._doc.track_changes.decide(
                {"decision": decision, "target": {"kind": "all"}}
            )
        except SuperDocError as exc:
            raise DocumentError(f"{decision} failed: {exc}") from exc

        if not receipt.get("success"):
            raise DocumentError(f"{decision} returned success=false: {receipt}")
        return receipt

    # --- output --------------------------------------------------------------

    def save(self, out: Path) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        self._doc.save({"out": str(out), "force": True})
        return out


@contextmanager
def open_document(path: Path) -> Iterator[DocumentSession]:
    """Open `path` for one unit of work.

    The session cannot outlive the process, so anything that must survive the
    human-approval pause has to be saved to disk before this context exits.
    """
    with SuperDocClient() as client:
        doc = client.open({"doc": str(path)})
        try:
            yield DocumentSession(doc)
        finally:
            doc.close({"discard": True})
