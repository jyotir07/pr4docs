"""The HTTP surface, including the bit that matters: the approval pause spanning two
requests and surviving a restart of the app."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pr4docs.api import create_app
from pr4docs.config import Settings
from pr4docs.deps import Deps
from tests.fakes import (
    FakeDocumentStore,
    ScriptedComposer,
    ScriptedPlanner,
    ScriptedValidator,
    edit,
    passing,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        storage=tmp_path / "storage",
        checkpoint_db=tmp_path / "checkpoints.sqlite",
        max_attempts=3,
    )


@pytest.fixture
def store() -> FakeDocumentStore:
    return FakeDocumentStore(autoseed=True)


@pytest.fixture
def deps(store: FakeDocumentStore, settings: Settings) -> Deps:
    return Deps(
        planner=ScriptedPlanner([[edit("s1", "b4")]]),
        composer=ScriptedComposer(),
        validator=ScriptedValidator([passing()]),
        open_document=store.opener(),
        settings=settings,
    )


@pytest.fixture
def client(deps: Deps, settings: Settings):
    with TestClient(create_app(deps=deps, settings=settings)) as c:
        yield c


def upload(client, docx: Path, request: str = "Make the introduction concise."):
    with docx.open("rb") as fh:
        return client.post(
            "/jobs",
            files={
                "file": (
                    docx.name,
                    fh,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data={"request": request},
        )


def test_upload_returns_a_diff_and_pauses(client, sample_docx):
    response = upload(client, sample_docx)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert "+ A concise rewrite." in body["diff"]
    assert len(body["changes"]) == 1
    assert body["output_ready"] is False


def test_approval_finalizes_and_the_document_downloads(client, sample_docx):
    thread_id = upload(client, sample_docx).json()["thread_id"]

    decided = client.post(f"/jobs/{thread_id}/decision", json={"approved": True})
    assert decided.status_code == 200
    assert decided.json()["status"] == "finalized"
    assert decided.json()["output_ready"] is True

    downloaded = client.get(f"/jobs/{thread_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    assert downloaded.content


def test_rejection_returns_a_fresh_proposal(client, sample_docx):
    thread_id = upload(client, sample_docx).json()["thread_id"]

    again = client.post(
        f"/jobs/{thread_id}/decision", json={"approved": False, "feedback": "too short"}
    )

    assert again.status_code == 200
    assert again.json()["status"] == "awaiting_approval"
    assert again.json()["output_ready"] is False


def test_reading_a_job_does_not_advance_it(client, sample_docx):
    thread_id = upload(client, sample_docx).json()["thread_id"]

    first = client.get(f"/jobs/{thread_id}").json()
    second = client.get(f"/jobs/{thread_id}").json()

    assert first == second
    assert first["status"] == "awaiting_approval"


def test_the_pause_survives_a_restart(deps, settings, sample_docx):
    """A new app over the same checkpoint file resumes a job it never saw created."""
    with TestClient(create_app(deps=deps, settings=settings)) as first:
        thread_id = upload(first, sample_docx).json()["thread_id"]

    with TestClient(create_app(deps=deps, settings=settings)) as second:
        assert second.get(f"/jobs/{thread_id}").json()["status"] == "awaiting_approval"

        decided = second.post(f"/jobs/{thread_id}/decision", json={"approved": True})
        assert decided.status_code == 200
        assert decided.json()["status"] == "finalized"
        assert second.get(f"/jobs/{thread_id}/download").status_code == 200


def test_app_starts_when_no_storage_directory_exists_yet(store, sample_docx, tmp_path):
    """First run on a clean machine: sqlite will not create intermediate directories."""
    fresh = Settings(
        storage=tmp_path / "nope" / "storage",
        checkpoint_db=tmp_path / "nope" / "db" / "checkpoints.sqlite",
        max_attempts=3,
    )
    deps = Deps(
        planner=ScriptedPlanner([[edit("s1", "b4")]]),
        composer=ScriptedComposer(),
        validator=ScriptedValidator([passing()]),
        open_document=store.opener(),
        settings=fresh,
    )

    with TestClient(create_app(deps=deps, settings=fresh)) as fresh_client:
        assert upload(fresh_client, sample_docx).status_code == 200


def test_unknown_job_is_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.post("/jobs/nope/decision", json={"approved": True}).status_code == 404
    assert client.get("/jobs/nope/download").status_code == 404


def test_deciding_twice_is_rejected(client, sample_docx):
    thread_id = upload(client, sample_docx).json()["thread_id"]
    client.post(f"/jobs/{thread_id}/decision", json={"approved": True})

    second = client.post(f"/jobs/{thread_id}/decision", json={"approved": True})

    assert second.status_code == 409
    assert "finalized" in second.json()["detail"]


def test_downloading_before_approval_is_rejected(client, sample_docx):
    thread_id = upload(client, sample_docx).json()["thread_id"]

    response = client.get(f"/jobs/{thread_id}/download")

    assert response.status_code == 409
    assert "awaiting_approval" in response.json()["detail"]


def test_non_docx_upload_is_rejected(client, tmp_path):
    note = tmp_path / "notes.txt"
    note.write_text("not a document")

    response = upload(client, note)

    assert response.status_code == 415


def test_empty_upload_is_rejected(client, tmp_path):
    empty = tmp_path / "empty.docx"
    empty.write_bytes(b"")

    assert upload(client, empty).status_code == 400
