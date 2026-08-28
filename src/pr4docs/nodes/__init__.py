"""Graph nodes. Each reads state, does one thing, and returns a state fragment.

Nodes are built as closures over `Deps` rather than importing collaborators directly,
which is what lets the whole state machine run against fakes.
"""

from pr4docs.nodes.builders import (
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

__all__ = [
    "make_analyze",
    "make_apply",
    "make_approval",
    "make_compose",
    "make_diff",
    "make_fail",
    "make_finalize",
    "make_plan",
    "make_preview",
    "make_validate",
]
