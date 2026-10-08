# SPDX-License-Identifier: AGPL-3.0
import httpx


class Client:
    """Small HTTP client. Use as a context manager to release sockets."""

    def __init__(self, url: str = "http://127.0.0.1:1933", api_key: str = "", timeout: float = 600):
        self.http = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=timeout,
            headers={"X-API-Key": api_key} if api_key else {},
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.http.close()

    def request(self, method: str, path: str, **kwargs):
        response = self.http.request(method, path, **kwargs)
        response.raise_for_status()
        body = response.json()
        return body.get("result", body)

    def add_text(self, text: str, to: str, *, summarize: bool = True):
        return self.request(
            "POST", "/api/v1/resources", json={"text": text, "to": to, "summarize": summarize}
        )

    def find(self, query: str, **kwargs):
        return self.request("POST", "/api/v1/search/find", json={"query": query, **kwargs})

    def ask(self, question: str, **kwargs):
        return self.request("POST", "/api/v1/ask", json={"question": question, **kwargs})

    def read(self, uri: str, level: int = 2):
        return self.request("GET", "/api/v1/content/read", params={"uri": uri, "level": level})

    def ls(self, uri: str = "viking://"):
        return self.request("GET", "/api/v1/fs/ls", params={"uri": uri})

    def rm(self, uri: str, *, recursive: bool = False):
        return self.request(
            "DELETE", "/api/v1/fs", params={"uri": uri, "recursive": str(recursive).lower()}
        )

    def create_session(self, user_id: str = "default", *, external_id: str | None = None):
        return self.request(
            "POST", "/api/v1/sessions", json={"user_id": user_id, "external_id": external_id}
        )

    def add_message(
        self, session_id: str, role: str, content: str, *, message_id: str | None = None
    ):
        return self.request(
            "POST",
            f"/api/v1/sessions/{session_id}/messages",
            json={"role": role, "content": content, "message_id": message_id},
        )

    def commit_session(self, session_id: str):
        return self.request("POST", f"/api/v1/sessions/{session_id}/commit")
