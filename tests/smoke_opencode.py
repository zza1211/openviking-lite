"""Actual OpenCode 1.x + actual Lite server/CPU embedding, simulated text APIs.

python tests/smoke_opencode.py --cache-dir ../OpenViking/lite/model-cache --opencode <opencode.exe>
Runs in a temporary project with isolated config/data and never calls a paid model.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from smoke_real_model import available_port

from openviking_lite import Client


def run(cache_dir: Path, opencode: str):
    chats, extractions = [], []

    class SimulatedAPI(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body.get("model") == "memory-model":
                extractions.append(body)
                content = json.dumps(
                    [{"category": "preferences", "text": "用户偏好简体中文和 Python 代码示例。"}],
                    ensure_ascii=False,
                )
            else:
                chats.append(body)
                content = "好的，我会使用简体中文和 Python 示例。"
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for delta, finish in [
                    ({"role": "assistant", "content": content}, None),
                    ({}, "stop"),
                ]:
                    chunk = {
                        "id": "chatcmpl-smoke",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": body.get("model"),
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                payload = json.dumps(
                    {
                        "id": "chatcmpl-smoke",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": body.get("model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

    api = ThreadingHTTPServer(("127.0.0.1", 0), SimulatedAPI)
    thread = threading.Thread(target=api.serve_forever, daemon=True)
    thread.start()
    processes = []
    try:
        with tempfile.TemporaryDirectory(prefix="lite-opencode-smoke-") as temp:
            temp = Path(temp)
            project = temp / "project"
            project.mkdir()
            lite_port, oc_port = available_port(), available_port()
            lite_url, oc_url = f"http://127.0.0.1:{lite_port}", f"http://127.0.0.1:{oc_port}"
            base = f"http://127.0.0.1:{api.server_port}/v1"
            config = {
                "workspace": str(temp / "lite-data"),
                "port": lite_port,
                "embedding": {"cache_dir": str(cache_dir.resolve()), "local_files_only": True},
                "llm": {"api_base": base, "model": "memory-model"},
            }
            config_path = temp / "lite-config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            root = Path(__file__).resolve().parent.parent
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            env = {
                k: v for k, v in os.environ.items() if not k.startswith(("OPENCODE_", "OV_LITE_"))
            }
            env.update(
                {
                    "HF_HUB_OFFLINE": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "XDG_CONFIG_HOME": str(temp / "config"),
                    "XDG_DATA_HOME": str(temp / "data"),
                    "XDG_STATE_HOME": str(temp / "state"),
                    "XDG_CACHE_HOME": str(temp / "cache"),
                    "OPENCODE_CONFIG_DIR": str(temp / "oc-config"),
                    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                    "OPENCODE_DISABLE_AUTOUPDATE": "true",
                }
            )
            subprocess.run(
                [
                    "node",
                    str(root / "integrations/opencode/install.mjs"),
                    "--project",
                    str(project),
                ],
                check=True,
                capture_output=True,
                env=env,
                creationflags=flags,
            )
            (project / ".opencode/openviking-lite.json").write_text(
                json.dumps(
                    {
                        "url": lite_url,
                        "user_id": "smoke",
                        "commit_debounce_ms": 0,
                        "recall_timeout_ms": 10000,
                    }
                ),
                encoding="utf-8",
            )
            oc_config = {
                "model": "lite-smoke/chat-model",
                "small_model": "lite-smoke/chat-model",
                "enabled_providers": ["lite-smoke"],
                "provider": {
                    "lite-smoke": {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": "Local simulated provider",
                        "options": {"baseURL": base, "apiKey": "test-only"},
                        "models": {
                            "chat-model": {
                                "name": "Chat smoke",
                                "limit": {"context": 32768, "output": 4096},
                            }
                        },
                    }
                },
            }
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(oc_config)
            logs = []
            try:
                for cmd, name, cwd in [
                    (
                        [
                            sys.executable,
                            "-m",
                            "openviking_lite.cli",
                            "serve",
                            "--config",
                            str(config_path),
                        ],
                        "lite",
                        root,
                    ),
                    (
                        [
                            opencode,
                            "serve",
                            "--port",
                            str(oc_port),
                            "--hostname",
                            "127.0.0.1",
                            "--print-logs",
                        ],
                        "opencode",
                        project,
                    ),
                ]:
                    log = (temp / f"{name}.log").open("w", encoding="utf-8")
                    logs.append(log)
                    processes.append(
                        subprocess.Popen(
                            cmd, cwd=cwd, stdout=log, stderr=log, env=env, creationflags=flags
                        )
                    )
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if any(p.poll() is not None for p in processes):
                        raise RuntimeError(
                            "Server exited: "
                            + (temp / "opencode.log").read_text(encoding="utf-8")[-6000:]
                        )
                    try:
                        if (
                            httpx.get(lite_url + "/health", timeout=1).is_success
                            and httpx.get(oc_url + "/global/health", timeout=1).is_success
                        ):
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Servers failed to start")
                with (
                    httpx.Client(
                        base_url=oc_url, timeout=60, params={"directory": str(project)}
                    ) as oc,
                    Client(lite_url) as lite,
                ):
                    first = oc.post("/session", json={"title": "memory capture smoke"})
                    first.raise_for_status()
                    session_id = first.json()["id"]
                    response = oc.post(
                        f"/session/{session_id}/message",
                        json={
                            "parts": [
                                {
                                    "type": "text",
                                    "text": "以后请用简体中文回答，代码示例优先用 Python。",
                                }
                            ],
                            "model": {"providerID": "lite-smoke", "modelID": "chat-model"},
                        },
                    )
                    response.raise_for_status()
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        found = lite.find("回答偏好", target_uri="viking://user/smoke/memories")
                        if found["total"]:
                            break
                        time.sleep(0.2)
                    else:
                        raise RuntimeError(
                            "Automatic extraction missing: "
                            + (temp / "opencode.log").read_text(encoding="utf-8")[-6000:]
                        )
                    with closing(
                        sqlite3.connect(temp / "lite-data/openviking-lite.db")
                    ) as database:
                        stored = database.execute(
                            "SELECT m.role, m.content FROM messages m "
                            "JOIN session_links l ON l.session_id=m.session_id "
                            "WHERE l.user_id=? AND l.external_id LIKE ? ORDER BY m.seq",
                            ("smoke", "%:" + session_id),
                        ).fetchall()
                    assert stored == [
                        ("user", "以后请用简体中文回答，代码示例优先用 Python。"),
                        ("assistant", "好的，我会使用简体中文和 Python 示例。"),
                    ], "Original user and assistant messages were not both persisted"
                    second = oc.post("/session", json={"title": "recall smoke"}).json()["id"]
                    before = len(chats)
                    response = oc.post(
                        f"/session/{second}/message",
                        json={
                            "parts": [{"type": "text", "text": "怎样遍历一个文件夹？"}],
                            "model": {"providerID": "lite-smoke", "modelID": "chat-model"},
                        },
                    )
                    response.raise_for_status()
                    assert any(
                        "<openviking-lite-memory>" in json.dumps(body, ensure_ascii=False)
                        for body in chats[before:]
                    ), "Recalled memory did not reach OpenCode model input"
                    assert all(
                        "<openviking-lite-memory>" not in body["messages"][1]["content"]
                        for body in extractions
                    ), "Injected memory was captured as original chat"
                    print(
                        json.dumps(
                            {
                                "status": "passed",
                                "opencode": "real installed executable",
                                "embedding": "real CPU ONNX",
                                "automatic_capture": True,
                                "persisted_roles": [role for role, _ in stored],
                                "automatic_extraction": True,
                                "recall_in_model_input": True,
                                "simulated_chat_requests": len(chats),
                                "simulated_extraction_requests": len(extractions),
                            },
                            indent=2,
                        )
                    )
            except Exception:
                print((temp / "opencode.log").read_text(encoding="utf-8")[-10000:], file=sys.stderr)
                raise
            finally:
                for process in reversed(processes):
                    process.terminate()
                    process.wait(timeout=10)
                for log in logs:
                    log.close()
                time.sleep(0.3)
    finally:
        api.shutdown()
        api.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=Path("model-cache"))
    parser.add_argument("--opencode", default=shutil.which("opencode") or "opencode")
    args = parser.parse_args()
    run(args.cache_dir, args.opencode)
