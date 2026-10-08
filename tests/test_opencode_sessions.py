import json

import pytest
from fastapi.testclient import TestClient
from test_lite import FakeEmbedder, FakeLLM

from openviking_lite.api import create_app
from openviking_lite.config import Settings
from openviking_lite.service import Service
from openviking_lite.store import ConflictError


@pytest.fixture
def service(tmp_path):
    svc = Service(Settings(workspace=tmp_path), embedder=FakeEmbedder(), llm=FakeLLM())
    yield svc
    svc.close()


def test_external_mapping_survives_restart_and_is_per_user(service):
    alice = service.create_session("alice", "opencode:project:session")
    assert service.create_session("alice", "opencode:project:session") == alice
    assert service.create_session("bob", "opencode:project:session") != alice
    reopened = Service(service.settings, embedder=FakeEmbedder(), llm=FakeLLM())
    try:
        assert reopened.create_session("alice", "opencode:project:session") == alice
    finally:
        reopened.close()


def test_message_idempotency_and_streaming_update_before_commit(service):
    sid = service.create_session()["session_id"]
    assert service.append_message(sid, "user", "中文", "msg-1") == {"seq": 1}
    assert service.append_message(sid, "user", "中文", "msg-1") == {"seq": 1}
    assert service.append_message(sid, "assistant", "partial", "msg-2") == {"seq": 2}
    assert service.append_message(sid, "assistant", "complete", "msg-2") == {"seq": 2}
    service.commit_session(sid)
    assert service.append_message(sid, "assistant", "complete", "msg-2") == {"seq": 2}
    with pytest.raises(ConflictError):
        service.append_message(sid, "assistant", "edited", "msg-2")
    assert len(service.store.session(sid)["messages"]) == 2


def test_changed_snapshot_does_not_commit_stale_extraction(service):
    sid = service.create_session()["session_id"]
    service.append_message(sid, "user", "before", "msg-1")
    snapshot = service.store.session(sid)
    service.append_message(sid, "user", "after", "msg-1")
    with pytest.raises(ConflictError, match="changed during"):
        service.store.commit_session(snapshot, [])
    assert service.store.session(sid)["committed_seq"] == 0


def test_long_and_escaped_messages_are_batched_without_losing_original(service):
    service.settings.llm.max_input_chars = 2000
    sid = service.create_session()["session_id"]
    text = '中文😀\n"\\\x01' * 2500
    service.append_message(sid, "user", text, "long-msg")
    blocks = list(service._message_blocks(service.store.session(sid)["messages"]))
    assert len(blocks) > 1
    assert all(len(block) <= 2000 for block in blocks)
    assert "".join(json.loads(block)["content"] for block in blocks) == text
    result = service.commit_session(sid)
    assert result["processed_messages"] == 1
    assert service.store.session(sid)["messages"][0]["content"] == text


def test_api_mapping_keys_and_conflict_code(service):
    with TestClient(create_app(service.settings, service=service)) as client:
        body = {"user_id": "alice", "external_id": "opencode:p:1"}
        first = client.post("/api/v1/sessions", json=body).json()["result"]
        assert client.post("/api/v1/sessions", json=body).json()["result"] == first
        path = f"/api/v1/sessions/{first['session_id']}/messages"
        message = {"role": "user", "content": "中文 Python", "message_id": "msg-1"}
        assert client.post(path, json=message).json()["result"] == {"seq": 1}
        assert client.post(path, json=message).json()["result"] == {"seq": 1}
        client.post(f"/api/v1/sessions/{first['session_id']}/commit")
        response = client.post(path, json={**message, "content": "changed"})
        assert response.status_code == 409
        assert response.json()["code"] == "committed_message_changed"
