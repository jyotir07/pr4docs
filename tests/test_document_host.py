"""The shared editor process: one client for the whole app, one session per open.

Starting a client per document open cost ~2.7s of process startup each time and blew
past the SDK's 5s startup timeout once four opens overlapped, so the client is started
once and reused. The SDK serialises calls on a client, so sharing one is safe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pr4docs.docs.superdoc import DocumentHost


class StubDoc:
    def __init__(self) -> None:
        self.closed_with: dict[str, Any] | None = None

    def close(self, params: dict[str, Any]) -> None:
        self.closed_with = params


class StubClient:
    instances: list[StubClient] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.connected = 0
        self.disposed = 0
        self.docs: list[StubDoc] = []
        StubClient.instances.append(self)

    def connect(self) -> None:
        self.connected += 1

    def open(self, params: dict[str, Any]) -> StubDoc:
        doc = StubDoc()
        self.docs.append(doc)
        return doc

    def dispose(self) -> None:
        self.disposed += 1


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> type[StubClient]:
    StubClient.instances = []
    monkeypatch.setattr("pr4docs.docs.superdoc.SuperDocClient", StubClient)
    return StubClient


def test_every_open_shares_one_client(stub_client: type[StubClient], tmp_path: Path) -> None:
    with DocumentHost() as host:
        with host.open(tmp_path / "a.docx"):
            pass
        with host.open(tmp_path / "b.docx"):
            pass

    assert len(stub_client.instances) == 1
    assert len(stub_client.instances[0].docs) == 2


def test_each_session_closes_its_document_but_not_the_client(
    stub_client: type[StubClient], tmp_path: Path
) -> None:
    with DocumentHost() as host:
        with host.open(tmp_path / "a.docx"):
            pass
        opened = stub_client.instances[0].docs[0]

        assert opened.closed_with == {"discard": True}
        assert stub_client.instances[0].disposed == 0

    assert stub_client.instances[0].disposed == 1


def test_a_failed_session_still_closes_its_document(
    stub_client: type[StubClient], tmp_path: Path
) -> None:
    host = DocumentHost()
    with host, pytest.raises(RuntimeError), host.open(tmp_path / "a.docx"):
        raise RuntimeError("node blew up")

    assert stub_client.instances[0].docs[0].closed_with == {"discard": True}


def test_the_host_starts_the_process_before_any_request(stub_client: type[StubClient]) -> None:
    """Started at boot, so a broken editor fails the app rather than the first job."""
    with DocumentHost():
        assert stub_client.instances[0].connected == 1


def test_the_startup_timeout_leaves_room_for_a_slow_start(stub_client: type[StubClient]) -> None:
    """Startup measured 1.6-4.6s alone and ~8s under load; the SDK default is 5s."""
    with DocumentHost():
        pass

    assert stub_client.instances[0].kwargs["startup_timeout_ms"] >= 30_000
