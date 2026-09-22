"""HTTP surface.

The approval pause spans two requests: POST /jobs runs until the graph interrupts and
returns the diff, then POST /jobs/{id}/decision resumes it. Nothing about the job lives
in this process between those calls — it is all in the checkpointer.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from pr4docs.config import Settings, get_settings
from pr4docs.deps import Deps
from pr4docs.docs.superdoc import DocumentHost
from pr4docs.graph import Compiled, build_graph
from pr4docs.state import PR4DocsState, initial_state

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class Decision(BaseModel):
    approved: bool
    feedback: str | None = Field(
        default=None, description="Why it was rejected. Fed straight to the planner."
    )


class JobView(BaseModel):
    thread_id: str
    status: str
    diff: str
    changes: list[dict[str, Any]]
    errors: list[str]
    output_ready: bool


def _view(thread_id: str, state: PR4DocsState) -> JobView:
    return JobView(
        thread_id=thread_id,
        status=state.get("status", "planning"),
        diff=state.get("diff", ""),
        changes=state.get("changes", []),
        errors=state.get("errors", []),
        output_ready=bool(state.get("output_path")),
    )


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def _load(graph: Compiled, thread_id: str) -> PR4DocsState:
    state = graph.get_state(_config(thread_id)).values
    if not state:
        raise HTTPException(404, f"no job {thread_id}")
    return cast(PR4DocsState, state)


def open_checkpointer(settings: Settings) -> SqliteSaver:
    # sqlite will not create intermediate directories, and a missing one fails startup
    # with a bare "unable to open database file"
    settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False is safe here: SqliteSaver guards writes with its own lock,
    # and sync endpoints run in starlette's threadpool rather than the event loop
    return SqliteSaver(sqlite3.connect(settings.checkpoint_db, check_same_thread=False))


def get_graph(request: Request) -> Compiled:
    """Module level on purpose: `from __future__ import annotations` makes FastAPI
    resolve Annotated[..., Depends(get_graph)] from a string, which cannot see a
    function nested inside create_app."""
    return cast(Compiled, request.app.state.graph)


def create_app(
    deps: Deps | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # imported here so the app can be built with fakes without needing an API key
        from pr4docs.roles import build_deps

        resolved_settings.ensure_dirs()
        saver = checkpointer or open_checkpointer(resolved_settings)

        # one editor process for the whole app, started here so a broken editor fails
        # startup rather than the first job. Injected deps bring their own opener.
        host = None
        if deps is None:
            host = DocumentHost()
            host.start()
            resolved_deps = build_deps(host.open)
        else:
            resolved_deps = deps

        app.state.graph = build_graph(resolved_deps, checkpointer=saver)
        app.state.settings = resolved_settings
        try:
            yield
        finally:
            if host is not None:
                host.close()

    app = FastAPI(title="PR4Docs", lifespan=lifespan)

    # status stays awaiting_approval until a resumed run reaches its next node, so the
    # status check alone cannot stop two decisions racing. In-process only: running
    # several workers would need this in the database instead.
    deciding: set[str] = set()
    deciding_lock = threading.Lock()

    # sync def, not async: graph.invoke blocks on an editor subprocess and LLM calls, so
    # starlette runs it in a worker thread instead of stalling the event loop
    @app.post("/jobs", response_model=JobView)
    def create_job(
        file: Annotated[UploadFile, File()],
        request: Annotated[str, Form(min_length=1)],
        graph: Annotated[Compiled, Depends(get_graph)],
    ) -> JobView:
        if not (file.filename or "").lower().endswith(".docx"):
            raise HTTPException(415, "only .docx is supported")

        payload = file.file.read()
        if not payload:
            raise HTTPException(400, "the uploaded file is empty")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"file exceeds {MAX_UPLOAD_BYTES} bytes")

        thread_id = uuid.uuid4().hex[:12]
        source = resolved_settings.uploads / f"{thread_id}.docx"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)

        state = graph.invoke(initial_state(str(source), request), _config(thread_id))
        return _view(thread_id, cast(PR4DocsState, state))

    @app.get("/jobs/{thread_id}", response_model=JobView)
    def read_job(thread_id: str, graph: Annotated[Compiled, Depends(get_graph)]) -> JobView:
        return _view(thread_id, _load(graph, thread_id))

    @app.post("/jobs/{thread_id}/decision", response_model=JobView)
    def decide(
        thread_id: str, decision: Decision, graph: Annotated[Compiled, Depends(get_graph)]
    ) -> JobView:
        with deciding_lock:
            if thread_id in deciding:
                raise HTTPException(409, f"a decision for job {thread_id} is already in progress")
            deciding.add(thread_id)

        try:
            state = _load(graph, thread_id)
            if state.get("status") != "awaiting_approval":
                raise HTTPException(
                    409, f"job {thread_id} is {state.get('status')}, not awaiting review"
                )

            resumed = graph.invoke(
                Command(resume={"approved": decision.approved, "feedback": decision.feedback}),
                _config(thread_id),
            )
            return _view(thread_id, cast(PR4DocsState, resumed))
        finally:
            with deciding_lock:
                deciding.discard(thread_id)

    @app.get("/jobs/{thread_id}/download")
    def download(thread_id: str, graph: Annotated[Compiled, Depends(get_graph)]) -> FileResponse:
        state = _load(graph, thread_id)
        output = state.get("output_path")
        if not output:
            raise HTTPException(
                409, f"job {thread_id} is {state.get('status')}; nothing to download"
            )

        path = Path(output)
        if not path.exists():
            raise HTTPException(410, "the finalized document is no longer on disk")

        return FileResponse(
            path,
            filename=f"{thread_id}.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    return app


app = create_app()
