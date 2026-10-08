"""Real CPU model + actual HTTP/CLI smoke test, with a local simulated chat API.

Run: python tests/smoke_real_model.py --cache-dir model-cache
The cache must already contain BAAI/bge-small-zh-v1.5; never downloads during this test.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from openviking_lite import Client


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(cache_dir: Path):
    calls = []

    class MockAPI(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/chat/completions"
            assert all(isinstance(m["content"], str) for m in body["messages"])
            calls.append(body)
            system = body["messages"][0]["content"]
            if "长期保存" in system:
                content = json.dumps(
                    [{"category": "preferences", "text": "用户偏好中文回答和 Python 示例。"}],
                    ensure_ascii=False,
                )
            elif "摘要" in system:
                content = json.dumps(
                    {
                        "abstract": "本地纯文本知识库",
                        "overview": "原文：" + body["messages"][1]["content"][:300],
                    },
                    ensure_ascii=False,
                )
            else:
                content = "项目不需要 GPU，使用 SQLite [1]。"
            data = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    mock = ThreadingHTTPServer(("127.0.0.1", 0), MockAPI)
    thread = threading.Thread(target=mock.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="openviking-lite-smoke-") as temp:
            temp = Path(temp)
            port = available_port()
            config = {
                "workspace": str(temp / "data"),
                "port": port,
                "embedding": {"cache_dir": str(cache_dir.resolve()), "local_files_only": True},
                "llm": {
                    "api_base": f"http://127.0.0.1:{mock.server_port}/v1",
                    "model": "simulated-text-api",
                },
            }
            config_path = temp / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            url = f"http://127.0.0.1:{port}"
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            env = {**os.environ, "HF_HUB_OFFLINE": "1"}
            with (temp / "server.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "openviking_lite.cli",
                        "serve",
                        "--config",
                        str(config_path),
                    ],
                    stdout=log,
                    stderr=log,
                    env=env,
                    creationflags=flags,
                )
                try:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError((temp / "server.log").read_text(encoding="utf-8"))
                        try:
                            if httpx.get(url + "/health", timeout=1).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("Server startup timed out")
                    with Client(url) as client:
                        client.add_text(
                            "OpenViking Lite 使用本地 CPU 向量模型和 SQLite。部署不需要 GPU 显卡。",
                            "viking://resources/deploy",
                        )
                        client.add_text(
                            "番茄鸡蛋面的做法：煮熟面条，加入炒好的番茄和鸡蛋。",
                            "viking://resources/cooking",
                            summarize=False,
                        )
                        found = client.find("部署需要 GPU 显卡吗？", level=2)
                        assert found["matches"][0]["uri"] == "viking://resources/deploy"
                        answer = client.ask("部署需要 GPU 吗？")
                        assert answer["sources"][0]["uri"] == "viking://resources/deploy"
                        sid = client.create_session("alice")["session_id"]
                        client.add_message(sid, "user", "请用中文回答，示例使用 Python。")
                        committed = client.commit_session(sid)
                        assert len(committed["memories"]) == 1
                        count = len(calls)
                        assert client.commit_session(sid)["processed_messages"] == 0
                        assert len(calls) == count
                        assert (
                            client.find("回答偏好", target_uri="viking://user/alice/memories")[
                                "total"
                            ]
                            == 1
                        )
                    cli = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "openviking_lite.cli",
                            "--url",
                            url,
                            "import",
                            str(Path(__file__).parent.parent / "README.md"),
                            "--to",
                            "viking://resources/guide",
                            "--no-summary",
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        env={**env, "PYTHONIOENCODING": "utf-8"},
                        timeout=60,
                        creationflags=flags,
                    )
                    assert cli.returncode == 0, cli.stderr
                    with Client(url) as client:
                        assert client.read("viking://resources/guide").startswith(
                            "# OpenViking Lite"
                        )
                        stats = client.request("GET", "/api/v1/system/status")
                        assert stats["documents"] == 4
                    print(
                        json.dumps(
                            {
                                "status": "passed",
                                "embedding": "real local CPU ONNX",
                                "text_api": "local simulated HTTP chat-completions",
                                "documents": stats["documents"],
                                "chunks": stats["chunks"],
                                "text_api_calls": len(calls),
                                "best_match": found["matches"][0],
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                finally:
                    process.terminate()
                    process.wait(timeout=10)
                    # Windows may release file handles just after process exit is observed.
                    time.sleep(0.2)
    finally:
        mock.shutdown()
        mock.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=Path("model-cache"))
    run(parser.parse_args().cache_dir)
