# SPDX-License-Identifier: AGPL-3.0
import json
import threading
import time

import httpx
import numpy as np

from .config import EmbeddingConfig, LLMConfig


class ModelError(RuntimeError):
    """Safe error to return to a client, without provider payloads or secrets."""


def normalize_vectors(vectors, count: int, dimension: int | None = None) -> np.ndarray:
    values = np.asarray(vectors, dtype=np.float32)
    if values.ndim != 2 or values.shape[0] != count or values.shape[1] < 1:
        raise ModelError("Embedding returned an invalid number or shape of vectors")
    if dimension is not None and values.shape[1] != dimension:
        raise ModelError("Embedding dimension changed; use a new workspace and re-import")
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if not np.isfinite(values).all() or not np.isfinite(norms).all() or (norms == 0).any():
        raise ModelError("Embedding returned non-finite or zero vectors")
    return values / norms


class LocalEmbedder:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._model = None
        self._lock = threading.Lock()
        self.identity = json.dumps(
            {
                "backend": "fastembed-cpu",
                "model": config.model,
                "query_prefix": config.query_prefix,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            try:
                self._model = TextEmbedding(
                    model_name=self.config.model,
                    cache_dir=str(self.config.cache_dir),
                    threads=self.config.threads,
                    providers=["CPUExecutionProvider"],
                    local_files_only=self.config.local_files_only,
                )
            except Exception as exc:
                raise ModelError(
                    "Cannot load local embedding model; run ov-lite download-model and check cache_dir"
                ) from exc
        return self._model

    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        if not texts:
            raise ValueError("Embedding input cannot be empty")
        if query:
            texts = [self.config.query_prefix + text for text in texts]
        with self._lock:
            try:
                values = list(self._get_model().embed(texts, batch_size=self.config.batch_size))
            except ModelError:
                raise
            except Exception as exc:
                raise ModelError("Local embedding inference failed") from exc
        return normalize_vectors(values, len(texts))


class TextModel:
    def __init__(self, config: LLMConfig, *, transport=None):
        self.config = config
        headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
        self._client = httpx.Client(
            timeout=config.timeout, headers=headers, transport=transport, follow_redirects=False
        )

    def close(self):
        self._client.close()

    def complete(self, system: str, text: str) -> str:
        if len(text) > self.config.max_input_chars:
            raise ValueError("Text exceeds llm.max_input_chars")
        # Plain strings only: no vision content, tools, provider SDK or JSON-mode requirement.
        body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
            "max_tokens": self.config.max_tokens,
        }
        for attempt in range(self.config.retries + 1):
            try:
                response = self._client.post(self.config.api_base + "/chat/completions", json=body)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.config.retries:
                        time.sleep(min(2**attempt, 4))
                        continue
                if response.is_error:
                    raise ModelError(f"Text API returned HTTP {response.status_code}")
                content = response.json()["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ModelError("Text API must return a non-empty message.content string")
                return content
            except httpx.TransportError as exc:
                if attempt == self.config.retries:
                    raise ModelError("Text API timed out or could not be reached") from exc
                time.sleep(min(2**attempt, 4))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ModelError("Text API returned an invalid chat-completions response") from exc
        raise ModelError("Text API request failed")

    def json(self, system: str, text: str):
        output = self.complete(system + "\n只输出有效 JSON，不要附加解释。", text).strip()
        # Accept a fenced JSON block, but do not repair malformed or truncated data silently.
        if output.startswith("```"):
            lines = output.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                output = "\n".join(lines[1:-1])
        try:
            return json.loads(output)
        except ValueError as exc:
            raise ModelError("Text API returned invalid JSON; no data has been committed") from exc
