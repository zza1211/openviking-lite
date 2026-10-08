import json
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from openviking_lite.api import create_app
from openviking_lite.cli import import_files, parser
from openviking_lite.config import LLMConfig, Settings, load_settings
from openviking_lite.models import ModelError, TextModel, normalize_vectors
from openviking_lite.service import Service, split_text
from openviking_lite.store import ConflictError, Store, canonical_uri


class FakeEmbedder:
    identity = "test-embedding-v1"

    def embed(self, texts, *, query=False):
        return normalize_vectors(
            [[1 + t.count("中文"), 1 + t.count("Python"), 1] for t in texts], len(texts)
        )


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.fail = False

    def json(self, system, text):
        self.calls.append(text)
        if self.fail:
            raise ModelError("Test provider failure")
        if "长期保存" in system:
            return [
                {"category": "preferences", "text": "用户希望用中文回答，代码示例使用 Python。"}
            ]
        return {"abstract": text[:100], "overview": text[:500]}

    def complete(self, system, text):
        self.calls.append(text)
        return "使用 Python [1]"

    def close(self):
        pass


@pytest.fixture
def service(tmp_path):
    svc = Service(Settings(workspace=tmp_path), embedder=FakeEmbedder(), llm=FakeLLM())
    yield svc
    svc.close()


def test_layers_update_restart_delete(service, tmp_path):
    uri = "viking://resources/test"
    original = "中文资料。\n" * 180
    result = service.add(uri, original)
    assert result["chunks"] > 3
    assert service.read(uri) == original
    assert service.read(uri + "/.abstract.md") == result["abstract"]
    assert service.read(uri, 1) == result["overview"]
    before = service.store.stats()["chunks"]
    service.add(uri, "新版 Python", summarize=False)
    assert service.store.stats()["documents"] == 1
    assert service.store.stats()["chunks"] < before
    other = Store(tmp_path, FakeEmbedder.identity)
    try:
        assert other.read(uri)["text"] == "新版 Python"
    finally:
        other.close()
    assert service.store.delete(uri, False) == 1
    assert service.store.stats()["chunks"] == 0
    with pytest.raises(KeyError):
        service.read(uri)


def test_scope_wildcards_case_and_document_dedup(service):
    for uri in [
        "viking://resources/a_b/one",
        "viking://resources/axb/two",
        "viking://resources/a_b_extra/three",
        "viking://resources/A_b/four",
    ]:
        service.add(uri, "中文 Python " * 100, summarize=False)
    found = service.find("中文", target_uri="viking://resources/a_b")
    assert [m["uri"] for m in found["matches"]] == ["viking://resources/a_b/one"]
    assert service.find("中文")["total"] == 4
    assert all(m["level"] == 2 for m in service.find("中文", level=2)["matches"])
    assert len(service.store.ls("viking://resources")) == 4
    assert "viking://resources/a_b" in service.read("viking://resources")
    with pytest.raises(ConflictError):
        service.store.delete("viking://resources", False)
    assert service.store.delete("viking://resources", True) == 4


@pytest.mark.parametrize(
    "uri",
    [
        "/tmp/file",
        "viking://resources/../x",
        "viking://resources/a//b",
        "viking://resources/x?image=1",
        "viking://resources/a%20b",
        "viking://other/name",
        "viking://resources/.abstract.md",
    ],
)
def test_invalid_uri(uri):
    with pytest.raises(ValueError):
        canonical_uri(uri)


def test_model_failure_keeps_old_document_and_vectors(service):
    uri = "viking://resources/keep"
    service.add(uri, "old", summarize=False)
    count = service.store.stats()
    service.llm.fail = True
    with pytest.raises(ModelError):
        service.add(uri, "new")
    assert service.read(uri) == "old"
    assert service.store.stats() == count


def test_vectors_invalid_and_model_change_guard(service, tmp_path):
    service.add("viking://resources/a", "Python", summarize=False)
    for value in [[[0, 0]], [[float("nan"), 1]], [[float("inf"), 1]]]:
        with pytest.raises(ModelError):
            normalize_vectors(value, 1)
    with pytest.raises(ValueError, match="model changed"):
        Store(tmp_path, "different-model-same-dimension")
    doc = {
        "uri": "viking://resources/b",
        "kind": "resource",
        "text": "bad",
        "abstract": "a",
        "overview": "b",
    }
    with pytest.raises(ModelError, match="dimension"):
        service.store.put(doc, [(2, "bad")], np.array([[1, 2]]))
    assert service.store.stats()["documents"] == 1


def test_long_summary_covers_end_and_chunks_cover_source(service):
    source = "中" * 20000 + "尾部事实"
    service.add("viking://resources/long", source)
    assert any("尾部事实" in call for call in service.llm.calls)
    parts = split_text(source, 400, 60)
    assert all(len(p) <= 400 for p in parts)
    assert parts[-1].endswith("尾部事实")
    # Small API budgets must still terminate even when model summaries are at the maximum length.
    service.settings.llm.max_input_chars = 2000
    service.llm.json = lambda *_: {"abstract": "a" * 140, "overview": "b" * 1200}
    assert service._summary("x" * 10000) == ("a" * 140, "b" * 500)


def test_sessions_idempotent_failure_incremental_and_atomic(service):
    sid = service.create_session("alice")["session_id"]
    service.append_message(sid, "user", "中文 Python")
    service.llm.fail = True
    with pytest.raises(ModelError):
        service.commit_session(sid)
    assert service.store.session(sid)["committed_seq"] == 0
    assert service.store.stats()["documents"] == 0
    service.llm.fail = False
    result = service.commit_session(sid)
    assert result["processed_messages"] == 1
    assert len(result["memories"]) == 1
    assert service.find("中文", target_uri="viking://user/alice/memories")["total"] == 1
    assert service.commit_session(sid)["processed_messages"] == 0
    snapshot = service.store.session(sid)
    service.append_message(sid, "user", "Python again")
    service.commit_session(sid)
    assert service.store.stats()["documents"] == 1  # Exact content deduplication.
    with pytest.raises(ConflictError):
        service.store.commit_session(snapshot, [])


def test_concurrent_messages_preserve_sequences(service):
    sid = service.create_session()["session_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: service.append_message(sid, "user", str(i)), range(20)))
    assert [m["seq"] for m in service.store.session(sid)["messages"]] == list(range(1, 21))


def test_invalid_memory_response_is_not_committed(service):
    sid = service.create_session()["session_id"]
    service.append_message(sid, "user", "text")
    service.llm.json = lambda *_: [{"category": "bad", "text": "fake"}]
    with pytest.raises(ModelError):
        service.commit_session(sid)
    assert service.store.session(sid)["committed_seq"] == 0


def test_ask_uses_details_and_source_refs(service):
    assert service.ask("Python")["sources"] == []
    service.add("viking://resources/a", "使用 Python", summarize=False)
    result = service.ask("使用什么语言？")
    assert "[1]" in result["answer"]
    assert result["sources"][0]["level"] == 2


def test_api_auth_and_end_to_end(service):
    settings = service.settings.model_copy(update={"server_api_key": "test-key"})
    with TestClient(create_app(settings, service=service)) as client:
        assert client.get("/health").status_code == 401
        client.headers["X-API-Key"] = "test-key"
        assert client.get("/health").json()["mode"] == "text-only"
        uri = "viking://resources/demo"
        assert (
            client.post("/api/v1/resources", json={"to": uri, "text": "中文 Python"}).status_code
            == 200
        )
        assert (
            client.get("/api/v1/content/read", params={"uri": uri}).json()["result"]
            == "中文 Python"
        )
        assert (
            client.get("/api/v1/content/read", params={"uri": uri, "level": "2"}).status_code == 200
        )
        assert (
            client.get("/api/v1/content/read", params={"uri": uri, "level": "0"}).status_code == 200
        )
        assert (
            client.get("/api/v1/content/read", params={"uri": uri, "level": "3"}).status_code == 422
        )
        assert (
            client.post("/api/v1/search/find", json={"query": "Python"}).json()["result"]["total"]
            == 1
        )
        assert (
            client.post(
                "/api/v1/search/find", json={"query": "x", "image_url": "x.png"}
            ).status_code
            == 422
        )
        assert (
            client.post("/api/v1/resources", json={"path": "C:/private.txt", "to": uri}).status_code
            == 422
        )
        sid = client.post("/api/v1/sessions", json={"user_id": "alice"}).json()["result"][
            "session_id"
        ]
        assert (
            client.post(
                f"/api/v1/sessions/{sid}/messages", json={"role": "user", "content": "中文"}
            ).status_code
            == 200
        )
        assert client.post(f"/api/v1/sessions/{sid}/commit").json()["result"]["memories"]
        assert (
            client.get(
                "/api/v1/content/read", params={"uri": "viking://resources/missing"}
            ).status_code
            == 404
        )
        assert client.delete("/api/v1/fs", params={"uri": uri}).json()["result"]["deleted"] == 1
        service.llm.fail = True
        assert client.post("/api/v1/resources", json={"to": uri, "text": "fail"}).status_code == 502


def test_provider_protocol_json_and_secret_redaction():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"abstract":"中文","overview":"Python"}\n```'
                        }
                    }
                ]
            },
        )

    model = TextModel(
        LLMConfig(api_base="https://example.test/v1", model="any-text-model", api_key="SECRET"),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert model.json("system", "text")["abstract"] == "中文"
        assert str(requests[0].url) == "https://example.test/v1/chat/completions"
        assert requests[0].headers["Authorization"] == "Bearer SECRET"
        body = json.loads(requests[0].content)
        assert body["model"] == "any-text-model"
        assert all(isinstance(m["content"], str) for m in body["messages"])
        assert "response_format" not in body
    finally:
        model.close()
    model = TextModel(
        LLMConfig(api_key="SECRET", retries=0),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(401, text="SECRET and private provider payload")
        ),
    )
    try:
        with pytest.raises(ModelError, match="HTTP 401") as exc:
            model.complete("s", "t")
        assert "SECRET" not in str(exc.value)
    finally:
        model.close()


def test_config_paths_env_and_strict_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API", "https://example.test/v1")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"llm": {"api_base": "${TEST_API}"}}))
    settings = load_settings(path)
    assert settings.workspace == tmp_path / "data"
    assert settings.embedding.cache_dir == tmp_path / "model-cache"
    assert settings.llm.api_base == "https://example.test/v1"
    path.write_text('{"llm":{"api_key":"${UNSET_LITE_TEST_VARIABLE}"}}')
    with pytest.raises(ValueError, match="Missing environment"):
        load_settings(path)
    with pytest.raises(ValueError):
        Settings(host="0.0.0.0")
    with pytest.raises(ValueError):
        Settings(chunk_overlap=400)
    with pytest.raises(ValueError):
        Settings(vlm={"model": "image-model"})


def test_import_filter_and_preserved_paths(tmp_path):
    calls = []

    class RecordingClient:
        def add_text(self, text, uri, **kwargs):
            calls.append((text, uri))
            return {"uri": uri}

    (tmp_path / "a.md").write_text("text", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    (tmp_path / ".env").write_text("secret")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.md").write_text("ignore")
    import_files(RecordingClient(), tmp_path, "viking://resources/project")
    assert calls == [("text", "viking://resources/project/a.md")]
    with pytest.raises(ValueError, match="Only supported text"):
        import_files(RecordingClient(), tmp_path / "image.png", "viking://resources/image")


def test_package_isolation_and_cli():
    assert not any(name == "openviking" or name.startswith("openviking.") for name in sys.modules)
    assert parser().parse_args(["ls"]).uri == "viking://"
    assert parser().parse_args(["read", "viking://resources/test"]).level == 2
