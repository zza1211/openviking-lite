# SPDX-License-Identifier: AGPL-3.0
import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmbeddingConfig(StrictConfig):
    model: str = "BAAI/bge-small-zh-v1.5"
    cache_dir: Path = Path("model-cache")
    local_files_only: bool = False
    threads: int = Field(default=2, ge=1, le=64)
    batch_size: int = Field(default=32, ge=1, le=256)
    query_prefix: str = "为这个句子生成表示以用于检索相关文章："


class LLMConfig(StrictConfig):
    api_base: str = "http://localhost:8000/v1"
    model: str = "text-model"
    api_key: str = ""
    timeout: float = Field(default=60, gt=0, le=600)
    retries: int = Field(default=2, ge=0, le=5)
    max_input_chars: int = Field(default=12000, ge=2000, le=100000)
    max_tokens: int = Field(default=1600, ge=256, le=8192)

    @field_validator("api_base")
    @classmethod
    def validate_base(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("llm.api_base must be an HTTP(S) API root, e.g. https://host/v1")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("llm.api_base must not contain credentials, query or fragment")
        return value.rstrip("/")


class Settings(StrictConfig):
    workspace: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = Field(default=1933, ge=1, le=65535)
    server_api_key: str = ""
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    chunk_size: int = Field(default=400, ge=100, le=450)
    chunk_overlap: int = Field(default=60, ge=0)
    max_document_chars: int = Field(default=1000000, ge=1000, le=5000000)

    @model_validator(mode="after")
    def validate_settings(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.server_api_key:
            raise ValueError("Set server_api_key when binding to a non-loopback address")
        return self


def load_settings(path: str | Path) -> Settings:
    """Paths are relative to the config file; ${VAR} must exist, never silently unset."""
    path = Path(path).expanduser().resolve()

    def expand(value):
        if isinstance(value, str):

            def replace(match):
                name = match.group(1)
                if name not in os.environ:
                    raise ValueError(f"Missing environment variable: {name}")
                return os.environ[name]

            return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    settings = Settings.model_validate(expand(json.loads(path.read_text(encoding="utf-8"))))
    for obj, attr in [(settings, "workspace"), (settings.embedding, "cache_dir")]:
        value = getattr(obj, attr).expanduser()
        setattr(obj, attr, (path.parent / value).resolve())
    return settings
