# PR4Docs — Build Plan (MVP)

## Context

`pr4docs_langgraph_project.md` specifies an AI document-editing agent: upload a `.docx`, describe a change in English, review a diff, approve, download. The repo is currently empty except that spec — this is greenfield.

The spec treats the document layer as a black box ("SuperDocs API") and assumes the diff and the retry loop must be built by hand. Research into the actual SuperDoc SDK changed the design materially, so this plan deviates from the spec in a few places. Every deviation is called out with its reason.

**Goal of this pass:** a working LangGraph state machine with a real validation/retry loop and a human-approval pause that survives an HTTP request boundary, driven through FastAPI with curl. No frontend, no Postgres, no Docker yet — those are additive and would mask bugs in the part that matters.

One prerequisite: this directory is not its own git repo (`git rev-parse --show-toplevel` → `C:/Users/jyoti`). Run `git init` here first.

---

## The design decision that shapes everything

SuperDoc's Document API is not a generic docx library — its primitives line up almost 1:1 with the nodes in the spec:

| Spec node | SuperDoc primitive | What it buys |
|---|---|---|
| Document Analyzer | `doc.query.match()` → targets + `evaluatedRevision` | Document-native addressing. No hand-rolled paragraph IDs. |
| Validator (structural) | `doc.mutations.preview(plan)` | **Deterministic, free, no mutation.** Returns `valid` + `failures` (step id, phase, code, message). |
| Editor | `doc.mutations.apply(plan)` with `atomic: true` | All-or-nothing. No partial application on failure. |
| Diff Generator | `trackChanges.list()` → `{id, type, author, before, after}` | The diff already exists as structured data. |
| Human Approval | `trackChanges.decide({decision, target})` | Approve/reject is a real document operation, not bookkeeping. |

Two consequences worth stating plainly:

1. **The retry loop gets machine-readable failure reasons.** `mutations.preview()` tells the planner *which step* failed and *why* before a single byte changes. That is a far stronger self-correction signal than an LLM judging its own output, and it costs nothing.
2. **Tracked changes give us free rollback.** Edits applied with `changeMode: 'tracked'` are suggestions. If semantic validation fails, `decide({decision: 'reject', target: {kind: 'all'}})` restores the original. We never need a document copy to roll back to.

---

## Architecture

```
POST /jobs ──> graph.invoke() ──> analyze → plan → compose → preview
                                              ↑                 │
                                              └── failures ─────┤ valid
                                              ↑                 ▼
                                              │              apply (tracked)
                                              │                 ▼
                                              └── rejected ── validate (LLM)
                                              ↑                 │ passed
                                              │                 ▼
                                              │               diff  ──> interrupt() ──> HTTP 200 {thread_id, diff}
                                              │                                              ⋮ (process may die here)
                                              └── feedback ─── reject ◄── POST /jobs/{id}/decision ──> Command(resume=)
                                                                          approve ──> finalize ──> save .docx
```

**Critical constraint the spec doesn't mention:** the SuperDoc document handle *cannot* live across the interrupt. The graph pauses, the HTTP request returns, the process may be restarted before approval arrives. So the working `.docx` (with tracked changes embedded) is saved to disk before the interrupt, and only its **path** goes into checkpointed state. On resume, the finalizer reopens that file. LangGraph state must stay JSON-serializable — no open handles, no client objects.

### Graph state

`src/pr4docs/state.py`:

```python
class PR4DocsState(TypedDict):
    thread_id: str
    source_path: str            # original upload, never mutated
    working_path: str | None    # docx carrying tracked changes
    request: str                # user's natural-language ask

    outline: list[dict]         # analyzer: [{ref, kind, preview_text}]
    plan: list[dict] | None     # planner: [{step_id, target_ref, op, instruction}]
    mutation_plan: dict | None  # composer: SuperDoc plan (atomic, tracked, expectedRevision)

    preview_failures: list[dict]   # from mutations.preview()
    changes: list[dict]            # trackChanges.list(): id/type/author/before/after
    diff: str                      # rendered for humans
    validation: dict | None        # {"passed": bool, "reason": str}

    revise_feedback: str | None    # rejection text OR validator/preview failure summary
    attempts: int                  # hard cap, see below
    approved: bool | None
    output_path: str | None
    errors: Annotated[list[str], operator.add]
```

### Nodes

**`analyze`** — open the doc, extract an outline of addressable targets (`ref` + preview text) plus `evaluatedRevision`. Truncate long documents by section for the planner prompt; keep the full outline in state.

**`plan`** *(LLM #1)* — structured output. Given `outline` + `request` (+ `revise_feedback` if retrying), emit *which* targets to change and *what* to do to each — instructions only, no final prose. Deviation from spec: splitting "where" from "what" means a failed preview can be re-planned for one step instead of regenerating everything.

**`compose`** *(LLM #2, one call per step, parallelizable)* — generate the replacement text for each target given its current text + instruction. Then assemble the SuperDoc mutation plan in code: `atomic: true`, `changeMode: 'tracked'`, `expectedRevision` from the analyzer, unique step `id`s. Op names must come from `doc.capabilities().planEngine.supportedStepOps` — do not hardcode.

**`preview`** — `doc.mutations.preview(plan)`. On `valid: false`, write `failures` into `revise_feedback` and route back to `plan`. Deterministic and free; this is the first line of defense.

**`apply`** — `doc.mutations.apply(plan)`, then `document.save()` to `working_path`. Check `receipt["success"]` before proceeding — the SuperDoc docs are explicit that a failed receipt must not be silently ignored.

**`validate`** *(LLM #3)* — semantic check only, since structure is already proven. Feed `trackChanges.list()` before/after pairs plus the original request: *did this actually do what was asked?* On failure: `decide({decision: 'reject', target: {kind: 'all'}})` to roll back, set `revise_feedback`, route to `plan`.

**`diff`** — render `changes` (`before`/`after` per change) into a readable unified-style string. Cheap, because the data is already structured.

**`approval`** — `interrupt({"diff": ..., "changes": ...})`. Nothing else in this node; it must be safe to re-execute from the top on resume (LangGraph re-runs the whole node when resuming).

**`finalize`** — reopen `working_path`, `track_changes.decide({"decision": "accept", "target": {"kind": "all"}})`, `document.save({"out": output_path, "force": True})`.

### Routing and the retry cap

`attempts` increments in `plan`. Cap at **3 total attempts**, then route to a terminal error state that returns the accumulated `errors` — an uncapped planner↔validator cycle is the single most expensive failure mode in this design.

Rejection routes back to `plan` with the user's feedback in `revise_feedback`, and rejects the outstanding tracked changes first so the next attempt starts from a clean document.

### API

`src/pr4docs/api.py` — four endpoints:

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/jobs` | multipart `file` + `request`; save upload, `graph.invoke()`, return `{thread_id, diff, changes}` once `__interrupt__` appears |
| `GET` | `/jobs/{thread_id}` | `graph.get_state(config)` snapshot |
| `POST` | `/jobs/{thread_id}/decision` | `{approved: bool, feedback?: str}` → `graph.invoke(Command(resume=...), config)` |
| `GET` | `/jobs/{thread_id}/download` | `FileResponse(output_path)` |

Checkpointer: `SqliteSaver` for now. Swapping to `PostgresSaver` later is a one-line change at compile time — that's the point of doing it in this order.

LLM: `init_chat_model("openai:gpt-...")` from LangChain, model id read from env. Provider-agnostic by config string, per my standing preference; no provider SDK imported directly outside `llm.py`.

### Layout

```
pyproject.toml
.env.example                    # OPENAI_API_KEY, PR4DOCS_MODEL, PR4DOCS_STORAGE
src/pr4docs/
  config.py  state.py  graph.py  llm.py  api.py
  nodes/     analyze.py planner.py composer.py editor.py validator.py differ.py approval.py finalizer.py
  docs/      superdoc.py         # the ONLY module that imports superdoc_sdk
storage/                         # uploads/, working/, output/
tests/
  fixtures/sample.docx
  test_superdoc_contract.py
  test_graph_flow.py
```

`docs/superdoc.py` is the single seam over the SDK — client lifecycle, `open`/`query`/`preview`/`apply`/`track_changes`/`save`, always closing with `document.close({"discard": True})`. Everything else in the graph talks to that wrapper, never to `superdoc_sdk` directly. This is also what makes the graph testable with a fake.

---

## Build order

**Step 0 — SuperDoc contract spike (do this first, before any graph code).**
`pip install superdoc-sdk`, then write `tests/test_superdoc_contract.py` against a real `.docx` fixture and confirm, in the installed version:
- sync vs async client (`SuperDocClient` / `AsyncSuperDocClient`) and process lifecycle under a long-lived server
- the Python method names for `query.match`, `mutations.preview`, `mutations.apply`, `track_changes.list/get/decide` — **the docs show most examples in JS**; the one Python example uses snake_case (`document.track_changes.decide`), so the Python surface is presumably snake_cased throughout, but that is an inference and must be verified, not assumed
- `doc.capabilities().planEngine.supportedStepOps` — the real op names
- the exact shape of a preview `failures` entry (this becomes planner feedback)

If any of this diverges from the plan, adjust before building on it. Everything downstream depends on this contract.

**Step 1** — `docs/superdoc.py` wrapper + a fake implementation for tests, driven by Step 0's findings.

**Step 2** — `state.py`, `graph.py` with all nodes stubbed, wired edges and conditional routing. Test the full topology with the fake and canned LLM responses: happy path, preview-failure retry, semantic-failure retry, retry-cap exhaustion, reject→revise, approve→finalize. **The state machine gets proven before a single real LLM call.**

**Step 3** — real analyzer, composer, and finalizer against the SuperDoc wrapper.

**Step 4** — real planner and validator LLM calls with structured output.

**Step 5** — FastAPI + SqliteSaver, and the interrupt/resume boundary across two separate HTTP requests.

Steps 2 and 5 are the interview-worthy parts. Steps 3–4 are where the time actually goes.

---

## Verification

Per step: `pytest` green before moving on. Ruff + mypy on `src/`.

End-to-end, on a real `.docx` with a multi-paragraph introduction:

```bash
# 1. propose
curl -F file=@tests/fixtures/sample.docx \
     -F request="Make the introduction about 40% shorter, keep the key points" \
     localhost:8000/jobs
# → {thread_id, diff, changes}

# 2. kill and restart the server here — this proves the pause is checkpointed, not in-memory

# 3. reject with feedback, confirm a NEW diff comes back
curl -X POST localhost:8000/jobs/$TID/decision \
     -d '{"approved": false, "feedback": "too aggressive, keep the second paragraph"}'

# 4. approve, download, open in Word
curl -X POST localhost:8000/jobs/$TID/decision -d '{"approved": true}'
curl -O -J localhost:8000/jobs/$TID/download
```

The download must open cleanly in Word with the edit applied and original formatting intact. Additionally: inspect the intermediate `working/*.docx` and confirm the tracked changes are visible as a native Word redline before acceptance — that is the strongest single signal the design works.

To exercise the retry loop deliberately, send a request naming a section that doesn't exist and confirm the preview failure reaches the planner and the attempt cap terminates cleanly.

---

## Known unknowns

- **Python SDK surface** — Step 0 resolves this. Planned against the documented JS API plus one Python example; the Python names are inferred, not confirmed.
- **Client concurrency** — the SDK manages a headless editor process. One client per request vs. a pool is unresolved; it needs a load check before this handles concurrent jobs. Not MVP-blocking, but don't ship it without deciding.
- **Checkpointer package names** — `SqliteSaver` has historically shipped as a separate `langgraph-checkpoint-sqlite` distribution. Confirm at install time.
- **AGPL-3.0** — fine for a portfolio project; a commercial license is required if this is ever distributed as a product.

---

## Commit checkpoints

I will not run `git commit` or `git push`. At each checkpoint below I stop, summarize what changed, and hand over a message to copy — then continue to the next step unless told otherwise.

| After | Covers | Suggested message |
|---|---|---|
| Scaffold | `git init`, `pyproject.toml`, layout, `.env.example`, `.gitignore` | `chore: scaffold pr4docs project structure and dependencies` |
| Steps 0–1 | SDK contract spike + `docs/superdoc.py` wrapper and fake | `feat: add SuperDoc document wrapper with SDK contract tests` |
| Step 2 | State schema, graph topology, routing, full flow tests against fakes | `feat: add LangGraph state machine with validation retry and approval loop` |
| Steps 3–4 | Real analyzer/composer/finalizer, planner and validator LLM calls | `feat: implement document analysis, edit planning and validation nodes` |
| Step 5 | FastAPI endpoints, SqliteSaver, interrupt/resume across requests | `feat: add FastAPI job endpoints with checkpointed human approval` |

Messages carry no trailers. If a step lands differently than planned, the message changes to match what was actually built.
