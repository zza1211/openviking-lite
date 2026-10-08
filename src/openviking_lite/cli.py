# SPDX-License-Identifier: AGPL-3.0
import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from .client import Client
from .config import load_settings
from .models import LocalEmbedder, ModelError, TextModel
from .store import canonical_uri

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".tsv",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".sh",
    ".ps1",
    ".sql",
    ".css",
    ".xml",
    ".ini",
    ".log",
}
IGNORED_DIRS = {".git", ".venv", ".venv-lite", "node_modules", "__pycache__", "model-cache", "data"}


def import_files(client: Client, path: Path, target: str, *, summarize: bool = True):
    path = path.expanduser().resolve()
    if not path.exists():
        raise ValueError(f"Import path does not exist: {path}")
    target = canonical_uri(target)
    if path.is_file() and path.suffix.lower() not in TEXT_EXTENSIONS:
        raise ValueError(
            "Only supported text/code files can be imported; PDF, Office and media are disabled"
        )
    if path.is_dir():
        files = []
        for folder, dirs, names in os.walk(path, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in IGNORED_DIRS
                and not d.startswith(".")
                and not (Path(folder) / d).is_symlink()
            )
            files.extend(
                Path(folder) / n
                for n in sorted(names)
                if not n.startswith(".")
                and Path(n).suffix.lower() in TEXT_EXTENSIONS
                and not (Path(folder) / n).is_symlink()
            )
    else:
        files = [path]
    if not files:
        raise ValueError("No supported text files found")
    results = []
    for file in files:
        if file.stat().st_size > 20000000:
            raise ValueError(f"File exceeds 20 MB: {file}")
        text = file.read_text(encoding="utf-8-sig")
        uri = target + "/" + file.relative_to(path).as_posix() if path.is_dir() else target
        results.append(client.add_text(text, uri, summarize=summarize))
    return results


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="OpenViking Lite: local text memory")
    root.add_argument("--url", default=os.environ.get("OV_LITE_URL", "http://127.0.0.1:1933"))
    root.add_argument("--api-key", default=os.environ.get("OV_LITE_API_KEY", ""))
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("serve", "download-model", "check"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="config.local.json")
        if name == "check":
            p.add_argument("--llm", action="store_true", help="Also call the configured text API")
    p = sub.add_parser("import")
    p.add_argument("path", type=Path)
    p.add_argument("--to", required=True)
    p.add_argument("--no-summary", action="store_true")
    p = sub.add_parser("write")
    p.add_argument("uri")
    p.add_argument("text")
    p.add_argument("--no-summary", action="store_true")
    for name in ("find", "ask"):
        p = sub.add_parser(name)
        p.add_argument("query")
        p.add_argument("--scope", default="viking://")
        p.add_argument("--limit", type=int, default=5)
    for name in ("read", "ls", "rm"):
        p = sub.add_parser(name)
        p.add_argument("uri", nargs="?" if name == "ls" else None, default="viking://")
        if name == "read":
            p.add_argument("--level", type=int, choices=[0, 1, 2], default=2)
        if name == "rm":
            p.add_argument("--recursive", action="store_true")
    sub.add_parser("status")
    return root


def main():
    args = parser().parse_args()
    try:
        if args.command in {"serve", "download-model", "check"}:
            settings = load_settings(args.config)
            if args.command == "serve":
                import uvicorn

                from .api import create_app

                uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1)
                return
            embedder = LocalEmbedder(settings.embedding)
            vector = embedder.embed(["本地中文向量模型测试"])
            result = {
                "embedding": "ok",
                "dimension": vector.shape[1],
                "cache_dir": str(settings.embedding.cache_dir),
            }
            if args.command == "check" and args.llm:
                llm = TextModel(settings.llm)
                try:
                    llm.complete("请简短回应。", "测试连接")
                    result["llm"] = "ok"
                finally:
                    llm.close()
        else:
            with Client(args.url, args.api_key) as client:
                if args.command == "import":
                    result = import_files(client, args.path, args.to, summarize=not args.no_summary)
                elif args.command == "write":
                    result = client.add_text(args.text, args.uri, summarize=not args.no_summary)
                elif args.command in {"find", "ask"}:
                    result = getattr(client, args.command)(
                        args.query, target_uri=args.scope, limit=args.limit
                    )
                elif args.command == "read":
                    result = client.read(args.uri, args.level)
                elif args.command == "ls":
                    result = client.ls(args.uri)
                elif args.command == "rm":
                    result = client.rm(args.uri, recursive=args.recursive)
                else:
                    result = client.request("GET", "/api/v1/system/status")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, ModelError, httpx.HTTPError) as exc:
        # ValidationError can include input values; print only its error messages.
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            message = "; ".join(
                ".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()
            )
        elif isinstance(exc, httpx.HTTPStatusError):
            message = f"Server returned HTTP {exc.response.status_code}: {exc.response.text[:500]}"
        elif isinstance(exc, httpx.HTTPError):
            message = "Cannot reach the server; check --url and whether ov-lite serve is running"
        else:
            message = str(exc)
        print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
