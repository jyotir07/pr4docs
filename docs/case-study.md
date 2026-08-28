# Tracked Changes Are the Diff

**Building a human-in-the-loop document editing agent on SuperDoc's Document API**

---

Every "AI edits your document" demo has the same three holes. The model returns prose, so formatting is gone. There is no reviewable unit — you get a new document, not a change to the old one. And there is no undo, because nothing recorded what was different.

Those aren't model problems. They're representation problems, and Word solved them decades ago. A tracked change is a first-class object in the file format: it has a before, an after, an author, and an accept/reject decision. It is, precisely, a diff with a merge button.

So the insight this project is built on is small and load-bearing:

> **Don't build a diff view. Emit tracked changes and let the document be the diff.**

SuperDoc's Document API exposes exactly that — plan a mutation, apply it in `tracked` mode, list the resulting changes as structured before/after pairs, and decide on them. What follows is what happened when I built an agent on top of that, what worked, and the three things I got wrong first.

The result is **PR4Docs**: upload a `.docx`, describe a change in English, review the redline, approve or send it back with a note, download the result.

![PR4Docs proposal open in Word — native tracked changes with Accept/Reject available in the Review ribbon](images/redline-in-word.png)

---

## The mapping

The reason this took days rather than weeks is that SuperDoc's primitives line up almost one-to-one with the nodes an editing agent needs. I did not have to build a document model.

| What the agent needs | SuperDoc primitive | What it buys |
|---|---|---|
| Address a target | `blocks.list` / `query.match` → node ids | Document-native addressing. No hand-rolled paragraph IDs, no line numbers, no fuzzy text matching. |
| Validate a plan | `mutations.preview(plan)` | **Deterministic, free, mutates nothing.** Returns `valid` plus a failure per step with a code and a message. |
| Apply an edit | `mutations.apply(plan)` with `atomic: true` | All-or-nothing. One bad target rejects the batch instead of half-applying it. |
| Produce a diff | `trackChanges.list()` | The diff already exists as structured data — `{id, type, author, before, after}`. Nothing to compute. |
| Accept or reject | `trackChanges.decide()` | Approval is a real document operation, not application bookkeeping. |

Two consequences are worth stating plainly, because both changed the architecture.

**The retry loop gets machine-readable failure reasons for free.** `preview()` tells the planner *which step* failed and *why* before a single byte changes. That is a far stronger self-correction signal than an LLM grading its own output, and it costs nothing — no tokens, no mutation, no rollback. So the deterministic check runs first and owns the verdict on whether a plan is well-formed. The LLM validator that runs later is asked only one question: *did this do what the user asked?*

**Tracked changes make rollback unnecessary.** Because edits applied in `tracked` mode are suggestions rather than facts, a failed attempt doesn't need to be undone — the working file is simply abandoned and the next attempt re-applies from the untouched original. There is no rollback path in this codebase because there is nothing to roll back.

---

## Architecture

```
analyze → plan → compose → preview ──valid──> apply → validate ──passed──> diff
            ↑                  │                          │                  ↓
            └──── failures ────┴──────────────────────────┘              approval
            ↑                                                            ╱      ╲
            └──────────────── reject ────────────────────────────── reject    approve
                                                                                 ↓
                                                                             finalize
```

A LangGraph state machine, three LLM calls in distinct roles, and one human pause.

**`analyze`** reads the document into an outline of addressable blocks. **`plan`** decides *which* blocks change and what should happen to each — instructions only, never prose. **`compose`** writes the replacement text for one block at a time, seeing that block's full text and nothing else. **`preview`** structurally validates. **`apply`** mutates atomically in tracked mode and saves. **`validate`** asks whether the request was actually satisfied. **`diff`** renders the change pairs. **`approval`** stops the graph and waits for a person. **`finalize`** accepts the tracked changes and saves the output.

Two design constraints shaped everything else.

**Planning and composing are separate calls.** The planner reads a *truncated* outline and decides where; the composer reads one block's *full* text and decides what. Fusing them means either paying for the entire document on every edit or writing prose from a 240-character preview. Splitting them also means a failed preview can be re-planned without regenerating text that was fine.

**The document handle cannot survive the pause.** The graph stops at `approval`, the HTTP request returns, and the process may be restarted before a decision arrives. So checkpointed state must stay JSON-serializable — paths, never open handles. The working `.docx`, tracked changes already embedded, is written to disk before the interrupt and reopened on resume.

That last one is testable, and I tested it across three separate server processes: process A accepted the upload and paused; A was killed; process B read the paused job from the checkpoint; process C approved it, finalized, and served the download. The pause is a property of the checkpoint, not of a running process.

![The server killed after upload and restarted — the paused job still returns from the checkpoint](images/pause-survives-restart.png)

---

## Three things I got wrong

The state machine was the easy part. These were not.

### 1. Feedback has to reach whoever made the mistake

The retry loop wouldn't converge. Asked to make a section 40% shorter, the agent would produce a 15% cut, get rejected, produce a 24% cut, get rejected, produce a 15% cut again, and exhaust its budget.

Rather than guess, I instrumented every component boundary and ran it once. The evidence was unambiguous: **the planner emitted byte-identical instructions on all three attempts.** Composer output wandered — 14.8%, then 1.9%, then 14.8% on the same paragraph — with no trend.

The cause was a routing error in my design. Rejection feedback went to the planner, and only to the planner. But a validator complaint is a complaint about *prose*, and the planner had targeted the right blocks every single time. The component that actually failed — the composer — saw an identical prompt on every attempt and, being deterministic at temperature 0 modulo sampling noise, did roughly the same thing.

The fix was to route validator feedback to the composer along with *what it wrote last time*, and to keep sending preview failures to the planner. Those are different failure classes: a preview failure genuinely *is* a targeting failure, which is why that path had worked all along.

The concrete previous attempt matters as much as the reason. Told only "too long," a model trims a few words and lands right back where it was rejected.

![The same instrumentation after the fix: the composer is handed the rejection and its own previous attempt, and the second pass clears the bar](images/converging-run.png)

*The instrumentation script is in the repo at `scripts/diagnose.py`. It wraps the three roles rather than replacing them, so what it prints is what production runs.*

### 2. Code measures; the model judges

With feedback routed correctly, a second failure surfaced. The validator looked at a **36% reduction** and called it *"roughly the same length as before."*

This is the same category error as asking a language model to do arithmetic. Length is a measurable property, so measurement belongs in Python. The validator now receives character counts and computed percentages per change, plus a section-wide total, and its system prompt tells it to trust the measurements over its own impression. Its job is narrowed to the thing it is actually good at: deciding whether a measured fact satisfies a stated request.

The section-wide total turned out to matter independently. A request like "make the Introduction 40% shorter" is about the *section*, not about each paragraph — without an aggregate figure, the validator was judging two paragraphs separately against a target that applied to their sum.

### 3. The design was right; the model was too weak

I isolated each change, one variable at a time, on the same 242-character paragraph and the same request:

| Variant | Result |
|---|---|
| Control (feedback to planner only, validator eyeballs length) | 242 → 185 chars — **23.6%** |
| Composer told why it was rejected, and what it wrote | 242 → 146 chars — **39.7%** |
| Composer given an explicit character budget | 242 → 140 chars — **42.1%** |
| Validator judging length by eye | *"length remains similar"* — false |
| Validator given measured lengths | *"only 35%"* — true |

Both fixes were necessary and both worked. And with both in place, `gpt-4o-mini` still failed: 13% → 25% → 27% across three attempts. Genuinely converging, but too slowly to finish inside the retry budget.

The identical code on `gpt-4o` converged in **two attempts**: 31% rejected with the true number, 42% accepted, straight to review. The design was correct; the model could not hold a quantitative rewrite target.

Two hypotheses I tested and did *not* ship, because the evidence didn't support them: a system-prompt rule instructing the composer to compute its own target length (5.7% → 13.8% → 5.7%, insufficient), and a tolerance clause letting the validator accept near-misses (results flip-flopped around the 35% boundary — too noisy to justify).

*Caveat on all of the above: one document, one request, one run per variant. These are directional findings from debugging, not a benchmark, and I'm not presenting them as one.*

---

## Field notes on the SDK

Written against **`superdoc-sdk` 2.7.0 on Windows**, Python. Offered in the spirit of being useful to whoever integrates next.

**The packaging is genuinely good.** The wheel bundles its own CLI companion, so `pip install superdoc-sdk` was the entire setup — no Node toolchain, no separate binary to locate. That is not the norm for document tooling and it saved real time.

**The Python surface needed discovery.** The published examples are largely JavaScript, and four details differed from what I inferred from them before I ran a spike against the installed package: the step operation names, the shape of the `args` payload, the discriminator inside `where`, and the namespace where capabilities live. None were hard to find — a contract spike against a real `.docx` resolved all four in an afternoon — but each was a guess that had to be corrected, and a short Python quickstart showing one complete plan end-to-end would have removed all of them at once.

Doing that spike *before* writing any agent code was the single best process decision in the project. Everything downstream depended on the contract, and I would have built on four wrong assumptions.

**One asymmetry worth documenting.** A *malformed* plan raises an exception, while a *well-formed but unsatisfiable* plan returns `valid: false` with failures. Both are the same event from the agent's point of view — "this plan is no good, here's why, try again" — so I normalize them into one shape at the SDK boundary. Without that, the retry loop only self-corrects for half its failure modes, which is a subtle way to half-build a feature. Surfacing both as failures, or documenting the split prominently, would help.

**An open question:** the SDK manages a headless editor process, and I could not determine from the docs whether the intended pattern under a long-lived server is one client per request or a shared pool. I've shipped one client per request and flagged concurrency as untested rather than guess. This is the thing I'd most like a maintainer's opinion on.

**License:** `superdoc-sdk` is AGPL-3.0. Fine for a personal project; distributing this as a product would require a commercial license, and I'd want to talk before doing that.

---

## What isn't solved

- **Concurrency is untested**, per above. It should not take parallel jobs until it's measured.
- **`SqliteSaver`** is the checkpointer. Swapping to Postgres is a one-line change at graph-compile time — that ordering was deliberate, not an accident.
- **No auth, no retention policy.** Uploads accumulate and every job is reachable by anyone who can reach the port.
- **One document, one request per job.** No batching, no multi-document operations.

---

## Stack

Python 3.11, LangGraph (state machine, checkpointing, `interrupt()`), LangChain (provider-agnostic model resolution, structured output), FastAPI, SuperDoc SDK, SQLite.

43 tests. 33 run by default with no API key and no editor subprocess — the SDK sits behind a single wrapper module and the three LLM roles are injected, so the entire state machine, every retry branch and the approval pause included, is testable with fakes. The remaining 10 are marked `contract` (real SDK) and `live` (real model) and are excluded from the default run.
