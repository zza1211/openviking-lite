import pytest
from fastapi.testclient import TestClient
from test_lite import FakeEmbedder, FakeLLM

from openviking_lite.api import create_app
from openviking_lite.config import Settings
from openviking_lite.service import Service


@pytest.fixture
def service(tmp_path):
    instance = Service(Settings(workspace=tmp_path), embedder=FakeEmbedder(), llm=FakeLLM())
    yield instance
    instance.close()


def test_web_shell_is_public_but_every_data_route_requires_auth(service):
    settings = service.settings.model_copy(update={"server_api_key": "private-test-key"})
    with TestClient(create_app(settings, service=service)) as client:
        for path in ["/", "/ui/", "/ui/app.js", "/ui/app.css"]:
            response = client.get(path)
            assert response.status_code == 200
            assert "private-test-key" not in response.text
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        uri = "viking://user/alice/memories/preferences/language"
        for path in [
            "/health",
            "/api/v1/system/status",
            "/api/v1/sessions",
            f"/api/v1/content/document?uri={uri}",
            f"/api/v1/fs/ls?uri={uri}",
        ]:
            assert client.get(path).status_code == 401
        assert (
            client.patch("/api/v1/content/document", params={"uri": uri}, json={"text": "x"})
        ).status_code == 401
        client.headers["X-API-Key"] = "private-test-key"
        assert client.get("/api/v1/sessions").status_code == 200


def test_document_editor_preserves_kind_reindexes_and_rejects_stale_changes(service):
    uri = "viking://user/alice/memories/preferences/language"
    service.add(uri, "中文", summarize=False)
    with TestClient(create_app(service.settings, service=service)) as client:
        original = client.get("/api/v1/content/document", params={"uri": uri}).json()["result"]
        body = {"text": "Python " * 200, "expected_updated_at": original["updated_at"]}
        edited = client.patch("/api/v1/content/document", params={"uri": uri}, json=body)
        assert edited.status_code == 200
        saved = edited.json()["result"]
        assert saved["kind"] == "memory"
        assert saved["created_at"] == original["created_at"]
        assert saved["text"] == body["text"]
        assert saved["abstract"] == body["text"].strip()[:140]
        assert service.llm.calls == []
        assert service.find("Python", target_uri=uri)["matches"][0]["content"].startswith("Python")
        before = service.store.stats()
        stale = client.patch(
            "/api/v1/content/document", params={"uri": uri}, json={**body, "text": "old edit"}
        )
        assert stale.status_code == 409
        assert service.read(uri) == body["text"]
        assert service.store.stats() == before


def test_missing_document_is_not_created_by_edit_and_directory_is_not_editable(service):
    service.add("viking://resources/notes/one", "safe", summarize=False)
    with TestClient(create_app(service.settings, service=service)) as client:
        for uri in ["viking://resources/missing", "viking://resources/notes"]:
            assert client.get("/api/v1/content/document", params={"uri": uri}).status_code == 404
            assert (
                client.patch(
                    "/api/v1/content/document", params={"uri": uri}, json={"text": "new"}
                ).status_code
                == 404
            )
        assert service.store.stats()["documents"] == 1


def test_sessions_are_filtered_paginated_and_show_incremental_extraction(service):
    alice = service.create_session("alice", "opencode:project:session")["session_id"]
    service.append_message(alice, "user", "偏好中文 Python")
    service.commit_session(alice)
    service.append_message(alice, "assistant", "好的")
    bob = service.create_session("bob")["session_id"]
    service.append_message(bob, "user", "a different conversation")
    with TestClient(create_app(service.settings, service=service)) as client:
        page = client.get("/api/v1/sessions", params={"user_id": "alice"}).json()["result"]
        assert page["total"] == 1
        saved = page["sessions"][0]
        assert saved["id"] == alice
        assert saved["message_count"] == 2
        assert saved["pending_messages"] == 1
        assert saved["external_id"] == "opencode:project:session"
        assert saved["preview"] == "偏好中文 Python"
        first = client.get("/api/v1/sessions", params={"limit": 1}).json()["result"]
        second = client.get("/api/v1/sessions", params={"limit": 1, "offset": 1}).json()["result"]
        assert first["total"] == second["total"] == 2
        assert first["sessions"][0]["id"] != second["sessions"][0]["id"]
        assert client.get("/api/v1/sessions", params={"offset": -1}).status_code == 422
        assert client.get("/api/v1/sessions", params={"user_id": "a/b"}).status_code == 422


def test_directory_can_contain_both_document_and_children(service):
    uri = "viking://resources/guide"
    service.add(uri, "父路径原文", summarize=False)
    service.add(uri + "/chapter", "章节", summarize=False)
    assert service.store.ls("viking://resources") == [
        {"uri": uri, "is_dir": True, "is_doc": True, "abstract": "父路径原文"}
    ]


def test_summary_failure_does_not_change_document_or_metadata(service):
    uri = "viking://skills/python"
    service.add(uri, "original", summarize=False)
    before = service.store.read(uri)
    service.llm.fail = True
    with TestClient(create_app(service.settings, service=service)) as client:
        response = client.patch(
            "/api/v1/content/document",
            params={"uri": uri},
            json={"text": "new", "summarize": True, "expected_updated_at": before["updated_at"]},
        )
        assert response.status_code == 502
        assert service.store.read(uri) == before
