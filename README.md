# PR4Docs

Pull requests, for Word documents.

Upload a `.docx`, describe the change you want in plain English, and get back a diff to review. Approve it and download the edited file; reject it with a note and the agent tries again. Every proposal is written into the document as **native Word tracked changes**, so the redline you review is the same one Word shows.

Built on LangGraph (the state machine, the retry loop, the human-in-the-loop pause) and the SuperDoc SDK (document addressing, atomic mutation plans, tracked changes).

---

## Quickstart

```bash
uv sync --extra dev
cp .env.example .env      # then put your OpenAI key in it
uv run uvicorn pr4docs.api:app --port 8000
```

On boot the app creates `storage/{uploads,working,output}` and opens `pr4docs.sqlite` for checkpoints.

Need a document to try it on? This writes the test fixture — a short quarterly report with an Introduction section — to `sample.docx`:

```bash
uv run python -c "import sys,pathlib; sys.path.insert(0,'tests'); from conftest import write_sample_docx; write_sample_docx(pathlib.Path('sample.docx'))"
```

### Drive it from the browser

Open <http://localhost:8000/docs>. The generated Swagger UI covers the whole loop and avoids shell quoting entirely:

1. **`POST /jobs`** — attach the `.docx`, type the request, Execute. This blocks for 20–90s: it is planning, composing, previewing, applying and validating, and it retries up to `PR4DOCS_MAX_ATTEMPTS` times if the validator isn't satisfied. The response carries the `diff` and a `thread_id`.
2. **`POST /jobs/{thread_id}/decision`** — `{"approved": false, "feedback": "..."}` sends it back for another pass with your note; `{"approved": true}` accepts.
3. **`GET /jobs/{thread_id}/download`** — the finished `.docx`.

### Drive it from the shell

```bash
curl -F file=@sample.docx \
     -F request="Make the Introduction about 40% shorter, keep the key points" \
     localhost:8000/jobs

TID=<thread_id from the response>

curl -X POST localhost:8000/jobs/$TID/decision \
     -H 'Content-Type: application/json' \
     -d '{"approved": false, "feedback": "too aggressive, keep the second paragraph"}'

curl -X POST localhost:8000/jobs/$TID/decision \
     -H 'Content-Type: application/json' -d '{"approved": true}'

curl -OJ localhost:8000/jobs/$TID/download
```

On Windows PowerShell, `curl` is an alias for `Invoke-WebRequest`, which takes different flags — use `curl.exe` for the upload and `Invoke-RestMethod ... -Body (@{approved=$true} | ConvertTo-Json)` for the JSON calls, or just use the browser.

### Where the files land

| Path | What's in it |
|---|---|
| `storage/uploads/` | the original upload, never mutated |
| `storage/working/` | the current proposal, carrying tracked changes |
| `storage/output/` | the finalized document, changes accepted |

**Open a file from `storage/working/` in Word before you approve.** The edits show up as a real redline with Accept/Reject in the ribbon — that is the clearest evidence the whole design works.

---

## API

| Method | Path | Behaviour |
|---|---|---|
| `POST` | `/jobs` | multipart `file` + `request`. Runs until the graph pauses for review. Returns `{thread_id, status, diff, changes, errors, output_ready}`. |
| `GET` | `/jobs/{thread_id}` | The current snapshot of that job. |
| `POST` | `/jobs/{thread_id}/decision` | `{approved: bool, feedback?: str}`. Resumes the paused graph. |
| `GET` | `/jobs/{thread_id}/download` | The finalized `.docx`. |

Errors: `415` on a non-`.docx`, `413` over 25 MB, `400` on an empty file, `404` on an unknown job, `409` when deciding on a job that isn't awaiting review or downloading one that isn't finished, `410` if the output file has been deleted.

A job that exhausts its retries ends with `status: "failed"` and the reasons in `errors`.

---

## How it works

```
analyze → plan → compose → preview ──valid──> apply → validate ──passed──> diff
            ↑                  │                          │                  ↓
            └──── failures ────┴──────────────────────────┘              approval
            ↑                                                            ╱      ╲
            └──────────────── reject ────────────────────────────── reject    approve
                                                                                 ↓
                                                                             finalize
```

| Node | What it does |
|---|---|
| `analyze` | Reads the document into an outline of addressable blocks (`node_id`, type, full text). |
| `plan` | **LLM.** Decides *which* blocks change and what should happen to each — instructions only, never the prose. |
| `compose` | **LLM.** Writes the replacement text for one block at a time, seeing that block's full text and nothing else. |
| `preview` | Structural validation via SuperDoc. Free, deterministic, mutates nothing. |
| `apply` | Applies the plan atomically with `changeMode: "tracked"`, saves the working file, reads back the changes. |
| `validate` | **LLM.** Semantic check only: did this actually do what was asked? |
| `diff` | Renders the before/after pairs into a reviewable diff. |
| `approval` | `interrupt()`. The graph stops here and the HTTP request returns. |
| `finalize` | Accepts all tracked changes and saves to `storage/output/`. |

### Five decisions worth explaining

**The document handle cannot survive the pause.** The graph stops at `approval` and the process may be restarted before a decision arrives, so state has to stay JSON-serializable — paths, never open handles. The working `.docx` (tracked changes already embedded) is written to disk before the interrupt and reopened on resume. This is why the pause survives `Ctrl+C`: kill the server after uploading, restart it, and `GET /jobs/{tid}` still returns the paused job.

**Planning and composing are separate calls.** The planner decides *where* and reads a truncated outline; the composer decides *what* and reads one block's full text. Asking one call to do both means either paying for the whole document on every edit or writing prose from a preview. The split also means a failed preview can be re-planned without regenerating everything.

**Deterministic validation runs before semantic validation.** `mutations.preview()` costs nothing, touches nothing, and returns a machine-readable failure per step. It is a far stronger self-correction signal than an LLM grading its own work, so it goes first and it is the only thing that judges whether a plan is well-formed. A hallucinated `node_id` is deliberately *not* filtered out in `plan` — it is allowed through so preview can reject it with a reason the planner can act on.

**Rejection feedback goes to the composer, not just the planner.** A validator complaint is a complaint about the *prose*, so it has to reach the component that wrote the prose, along with what that component wrote last time. Routing it only to the planner cannot converge: the planner re-emits the same instruction and the composer, seeing an unchanged prompt, repeats itself. This was a real bug — it looked like a weak model until the boundaries were instrumented.

**Code measures, the model judges.** Asked to eyeball length, the validator called a 36% reduction "roughly the same length as before". Character counts and percentages are computed in Python and handed to the validator as facts; the model only decides whether the measurement satisfies the request.

### Retries

`attempts` increments in `plan` and is capped at `PR4DOCS_MAX_ATTEMPTS`. Every loop back re-applies from the untouched source, so a failed proposal needs no rollback — its working file is just abandoned.

A **human** rejection resets the budget to zero. The cap exists to stop runaway LLM loops, and a person clicking reject is not one.

---

## Layout

```
src/pr4docs/
  api.py            FastAPI app, SqliteSaver, the interrupt/resume boundary
  graph.py          the state machine: nodes, edges, routing
  state.py          the checkpointed state schema
  deps.py           injected seams — three LLM roles + the document opener
  roles.py          real planner / composer / validator, prompts and structured output
  llm.py            provider-agnostic model resolution
  config.py         settings
  nodes/            node implementations
  docs/superdoc.py  the ONLY module that imports the SDK
```

Two seams make the whole thing testable. `docs/superdoc.py` is the single boundary over SuperDoc, and `Deps` injects the three LLM roles — so the entire state machine, every branch and retry and the approval pause included, runs in tests with no API key and no editor subprocess.

---

## Development

```bash
uv run pytest                 # 33 tests, no API key, no subprocess, ~seconds
uv run pytest -m contract     # 8 tests against the real SuperDoc SDK; slow
uv run pytest -m live         # 2 tests against the real LLM; costs money
uv run ruff check . && uv run ruff format --check .
uv run mypy                   # strict, no ignores in the codebase
```

`contract` and `live` are excluded from the default run by `addopts`.

## Configuration

Read from `.env` (see `.env.example`).

| Variable | Default | Notes |
|---|---|---|
| `PR4DOCS_MODEL` | `openai:gpt-4o` | Provider-agnostic id, resolved by `init_chat_model`. |
| `OPENAI_API_KEY` | — | Read from the environment by the provider SDK. |
| `PR4DOCS_STORAGE` | `./storage` | Parent of `uploads/`, `working/`, `output/`. |
| `PR4DOCS_CHECKPOINT_DB` | `./pr4docs.sqlite` | LangGraph checkpoints. |
| `PR4DOCS_MAX_ATTEMPTS` | `3` | Automated retries before a job fails. |

On the model: `gpt-4o-mini` cannot hold a quantitative rewrite target. On "40% shorter" it stalls around 27% and burns the whole retry budget; `gpt-4o` converges in two attempts. Only the **composer** needs the stronger model — the planner and validator both do fine on `mini`, and `Deps` takes the three roles separately if you want to split them.

## Known limits

- **Concurrency is untested.** The SDK manages a headless editor process, and one client per request versus a pool is unresolved. Don't put concurrent jobs through this without measuring it first.
- **`SqliteSaver`.** Swapping to `PostgresSaver` is a one-line change where the graph is compiled; that ordering was deliberate.
- **No auth, no cleanup.** `storage/` grows without bound and every job is reachable by anyone who can reach the port.
- **`superdoc-sdk` is AGPL-3.0.** Fine for a personal or portfolio project; distributing this as a product needs a commercial license.
