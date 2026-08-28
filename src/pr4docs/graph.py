"""The state machine.

    analyze → plan → compose → preview ──valid──> apply → validate ──passed──> diff
                ↑                  │                          │                  ↓
                └──── failures ────┴──────────────────────────┘              approval
                ↑                                                            ╱      ╲
                └──────────────── reject ────────────────────────────── reject    approve
                                                                                     ↓
                                                                                 finalize

Every loop back to `plan` re-applies from the untouched source, so no rollback is
needed — the working file from the failed attempt is simply abandoned.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from pr4docs.deps import Deps
from pr4docs.nodes import (
    make_analyze,
    make_apply,
    make_approval,
    make_compose,
    make_diff,
    make_fail,
    make_finalize,
    make_plan,
    make_preview,
    make_validate,
)
from pr4docs.state import PR4DocsState


def _exhausted(state: PR4DocsState, deps: Deps) -> bool:
    return state.get("attempts", 0) >= deps.settings.max_attempts


Compiled = CompiledStateGraph[PR4DocsState, None, PR4DocsState, PR4DocsState]


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Compiled:
    builder = StateGraph(PR4DocsState)

    builder.add_node("analyze", make_analyze(deps))
    builder.add_node("plan", make_plan(deps))
    builder.add_node("compose", make_compose(deps))
    builder.add_node("preview", make_preview(deps))
    builder.add_node("apply", make_apply(deps))
    builder.add_node("validate", make_validate(deps))
    builder.add_node("diff", make_diff(deps))
    builder.add_node("approval", make_approval(deps))
    builder.add_node("finalize", make_finalize(deps))
    builder.add_node("fail", make_fail(deps))

    builder.add_edge(START, "analyze")

    def after_analyze(state: PR4DocsState) -> str:
        return "plan" if state.get("outline") else "fail"

    def after_plan(state: PR4DocsState) -> str:
        # an empty plan means the planner had nothing to say; retrying rarely helps
        return "compose" if state.get("plan") else "fail"

    def after_preview(state: PR4DocsState) -> str:
        if not state.get("preview_failures"):
            return "apply"
        return "fail" if _exhausted(state, deps) else "plan"

    def after_apply(state: PR4DocsState) -> str:
        if state.get("changes"):
            return "validate"
        return "fail" if _exhausted(state, deps) else "plan"

    def after_validate(state: PR4DocsState) -> str:
        validation = state.get("validation") or {}
        if validation.get("passed"):
            return "diff"
        return "fail" if _exhausted(state, deps) else "plan"

    def after_approval(state: PR4DocsState) -> str:
        return "finalize" if state.get("approved") else "plan"

    builder.add_conditional_edges("analyze", after_analyze, ["plan", "fail"])
    builder.add_conditional_edges("plan", after_plan, ["compose", "fail"])
    builder.add_edge("compose", "preview")
    builder.add_conditional_edges("preview", after_preview, ["apply", "plan", "fail"])
    builder.add_conditional_edges("apply", after_apply, ["validate", "plan", "fail"])
    builder.add_conditional_edges("validate", after_validate, ["diff", "plan", "fail"])
    builder.add_edge("diff", "approval")
    builder.add_conditional_edges("approval", after_approval, ["finalize", "plan"])
    builder.add_edge("finalize", END)
    builder.add_edge("fail", END)

    return builder.compile(checkpointer=checkpointer)
