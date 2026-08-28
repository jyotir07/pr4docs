# PR4Docs — AI Document Editing Agent

## Project Description

**PR4Docs** is an AI-powered document editing tool where a user uploads a document, describes the changes they want in natural language, reviews the generated diff, approves the changes, and downloads the updated document.

### Core Flow

```text
Upload Document
      ↓
Describe Changes
      ↓
Analyze Document
      ↓
Plan Edits
      ↓
Apply Edits
      ↓
Validate Result
      ↓
Generate Diff
      ↓
Human Approval
   ↙       ↘
Reject     Approve
  ↓          ↓
Revise     Finalize
             ↓
       Download Document
```

---

## How LangGraph Fits In

PR4Docs should use **LangGraph as the workflow/orchestration layer**, rather than using an LLM as one giant function.

The document-editing process naturally has multiple states, decisions, and loops, which is exactly where LangGraph is useful.

### Graph State

A shared state can contain:

```python
{
    "doc": ...,
    "request": "...",
    "plan": ...,
    "edits": ...,
    "validation": ...,
    "diff": ...,
    "approved": False,
    "errors": []
}
```

Each LangGraph node reads the state, performs one responsibility, and updates the state.

### Main Nodes

#### 1. Document Analyzer

Extracts the document structure and relevant context.

**Input:** uploaded document  
**Output:** structured document representation

#### 2. Edit Planner

Uses an LLM to convert the user's natural-language request into explicit edit operations.

Example:

```text
"Change the introduction to be more concise"

→
[
  {
    "section": "introduction",
    "operation": "rewrite",
    "instruction": "Make it 40% shorter while preserving key points"
  }
]
```

#### 3. Editor

Applies the planned operations to the document using the document-processing layer/API.

#### 4. Validator

Checks whether the resulting document actually satisfies the requested changes and whether the document remains valid.

If validation fails, the graph can route back to the planner/editor.

```text
Editor → Validator
           ↓
        failed
           ↓
        Planner
```

This creates a **self-correcting workflow** instead of blindly trusting the first LLM output.

#### 5. Diff Generator

Creates a human-readable before/after diff showing exactly what changed.

#### 6. Human Approval

The graph pauses and waits for the user to approve or reject the proposed changes.

```text
             ┌── Reject → Revise
             ↓
Diff → Human Approval
             ↓
           Approve
             ↓
         Finalize
```

This is one of the strongest reasons to use LangGraph: the workflow can maintain state across a human-in-the-loop interruption.

#### 7. Finalizer

Once approved, generates the final document and returns it to the user.

---

## Why LangGraph Instead of Just LangChain?

**LangChain** can handle individual LLM calls, tool calls, document loaders, structured outputs, and retrieval.

**LangGraph** is better suited for orchestrating the complete PR4Docs workflow because it provides:

- Stateful execution
- Conditional branching
- Loops/retries
- Human-in-the-loop pauses
- Persistent workflow state
- Multiple specialized agents/nodes
- Better control over complex agent behavior

A simple implementation could use:

```text
LangChain
   ↓
LLM + document tools + structured outputs

LangGraph
   ↓
Planner → Editor → Validator → Diff → Human Approval
              ↑          |
              └──────────┘
```

---

## Suggested Architecture

```text
Frontend
   │
   ▼
FastAPI Backend
   │
   ▼
LangGraph
   │
   ├── Document Analyzer
   ├── Edit Planner
   ├── Editor Tool
   ├── Validator
   ├── Diff Generator
   └── Human Approval
   │
   ├── LLM API
   ├── Document Processing / SuperDocs API
   └── PostgreSQL / Checkpoint Store
```

### Tech Stack

- **Python**
- **LangGraph** — workflow orchestration
- **LangChain** — LLM/tool abstractions
- **FastAPI** — backend API
- **OpenAI API** — reasoning/edit planning
- **SuperDocs API** — document manipulation
- **PostgreSQL** — document/job metadata and checkpoints
- **React/Next.js** — frontend
- **Docker** — deployment

---

## Example Interview Explanation

> “PR4Docs is a stateful AI document-editing agent. I used LangGraph to represent the editing process as a graph instead of making one large LLM call. The graph analyzes the document, plans structured edits, applies them through tools, validates the result, generates a diff, and then pauses for human approval. If validation fails, it can loop back and revise the edits. This gave me explicit control over state, branching, retries, and human-in-the-loop execution.”

---

## MVP Scope

For the first version, keep it small:

1. Upload `.docx`
2. Enter natural-language edit request
3. Analyze document
4. Generate structured edit plan
5. Apply edits
6. Validate
7. Show before/after diff
8. Approve or reject
9. Download final `.docx`

The key interview-worthy feature is **the LangGraph state machine + validation/retry + human approval loop**, not the UI.
