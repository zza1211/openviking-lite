# SPDX-License-Identifier: AGPL-3.0
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .models import normalize_vectors


class ConflictError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_uri(uri: str, *, root: bool = False) -> str:
    if not uri.startswith("viking://"):
        raise ValueError("URI must start with viking://")
    tail = uri[len("viking://") :].rstrip("/")
    if not tail and root:
        return "viking://"
    segments = tail.split("/")
    if segments[0] not in {"resources", "user", "skills"}:
        raise ValueError("URI root must be resources, user or skills")
    if any(not s or s in {".", ".."} for s in segments):
        raise ValueError("URI cannot contain empty, . or .. segments")
    if any(ord(c) < 32 or c in "\\?#%" for c in tail):
        raise ValueError("URI contains a forbidden character")
    if not root and (len(segments) < 2 or segments[-1].startswith(".")):
        raise ValueError("Provide a document URI, e.g. viking://resources/notes")
    return "viking://" + tail


def scope_clause(uri: str) -> tuple[str, tuple]:
    uri = canonical_uri(uri, root=True)
    if uri == "viking://":
        return "1=1", ()
    prefix = uri.replace("!", "!!").replace("_", "!_") + "/%"
    return "(uri=? OR uri LIKE ? ESCAPE '!')", (uri, prefix)


class Store:
    def __init__(self, workspace: Path, embedding_identity: str):
        workspace.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            workspace / "openviking-lite.db", check_same_thread=False, timeout=30
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA case_sensitive_like=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (
                uri TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL,
                abstract TEXT NOT NULL, overview TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                uri TEXT NOT NULL REFERENCES documents(uri) ON DELETE CASCADE,
                chunk_id INTEGER NOT NULL, level INTEGER NOT NULL, text TEXT NOT NULL,
                vector BLOB NOT NULL, PRIMARY KEY(uri, chunk_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                committed_seq INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                PRIMARY KEY(session_id, seq)
            );
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")
            self.db.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('embedding_identity', ?)",
                (embedding_identity,),
            )
        values = dict(self.db.execute("SELECT key, value FROM metadata"))
        if values["schema_version"] != "1" or values["embedding_identity"] != embedding_identity:
            self.db.close()
            raise ValueError(
                "Workspace schema or embedding model changed; use a new workspace and re-import"
            )

    def close(self):
        with self._lock:
            self.db.close()

    def _put(self, document: dict, chunks: list[tuple[int, str]], vectors: np.ndarray):
        row = self.db.execute("SELECT value FROM metadata WHERE key='dimension'").fetchone()
        dimension = int(row[0]) if row else None
        vectors = normalize_vectors(vectors, len(chunks), dimension)
        if dimension is None:
            self.db.execute(
                "INSERT INTO metadata VALUES ('dimension', ?)", (str(vectors.shape[1]),)
            )
        stamp = now()
        self.db.execute(
            """
            INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uri) DO UPDATE SET kind=excluded.kind, text=excluded.text,
                abstract=excluded.abstract, overview=excluded.overview, updated_at=excluded.updated_at
        """,
            (
                document["uri"],
                document["kind"],
                document["text"],
                document["abstract"],
                document["overview"],
                stamp,
                stamp,
            ),
        )
        self.db.execute("DELETE FROM chunks WHERE uri=?", (document["uri"],))
        self.db.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
            [
                (document["uri"], i, level, text, vector.astype("<f4").tobytes())
                for i, ((level, text), vector) in enumerate(zip(chunks, vectors, strict=True))
            ],
        )

    def put(self, document: dict, chunks: list[tuple[int, str]], vectors: np.ndarray):
        with self._lock, self.db:
            self._put(document, chunks, vectors)

    def read(self, uri: str) -> dict:
        with self._lock:
            row = self.db.execute("SELECT * FROM documents WHERE uri=?", (uri,)).fetchone()
            if row is None:
                raise KeyError(uri)
            return dict(row)

    def ls(self, uri: str) -> list[dict]:
        clause, args = scope_clause(uri)
        prefix = uri if uri.endswith("/") else uri + "/"
        with self._lock:
            rows = self.db.execute(
                f"SELECT uri, kind, abstract FROM documents WHERE {clause} ORDER BY uri", args
            ).fetchall()
        children = {}
        for row in rows:
            if row["uri"] == uri:
                continue
            suffix = row["uri"][len(prefix) :]
            name = suffix.split("/", 1)[0]
            child_uri = prefix + name
            if child_uri not in children:
                children[child_uri] = {
                    "uri": child_uri,
                    "is_dir": "/" in suffix,
                    "abstract": row["abstract"] if "/" not in suffix else "",
                }
            elif "/" in suffix:
                children[child_uri]["is_dir"] = True
        return list(children.values())

    def delete(self, uri: str, recursive: bool) -> int:
        clause, args = scope_clause(uri)
        with self._lock, self.db:
            rows = self.db.execute(f"SELECT uri FROM documents WHERE {clause}", args).fetchall()
            if not rows:
                raise KeyError(uri)
            if not recursive and any(row["uri"] != uri for row in rows):
                raise ConflictError("URI has children; explicitly set recursive=true")
            self.db.execute(f"DELETE FROM documents WHERE {clause}", args)
            return len(rows)

    def search(
        self, vector: np.ndarray, uri: str, limit: int, threshold: float, level: int | None = None
    ) -> list[dict]:
        clause, args = scope_clause(uri)
        if level is not None:
            clause += " AND level=?"
            args += (level,)
        with self._lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key='dimension'").fetchone()
            query = normalize_vectors([vector], 1, int(row[0]) if row else None)[0]
            cursor = self.db.execute(
                f"SELECT uri, chunk_id, level, text, vector FROM chunks WHERE {clause}", args
            )
            best = {}
            while rows := cursor.fetchmany(512):
                matrix = np.stack([np.frombuffer(r["vector"], dtype="<f4") for r in rows])
                for item, score in zip(rows, matrix @ query, strict=True):
                    score = float(np.clip(score, -1, 1))
                    if score >= threshold and (
                        item["uri"] not in best or score > best[item["uri"]]["score"]
                    ):
                        best[item["uri"]] = {
                            "uri": item["uri"],
                            "score": score,
                            "chunk_id": item["chunk_id"],
                            "level": item["level"],
                            "content": item["text"],
                        }
            results = sorted(best.values(), key=lambda r: (-r["score"], r["uri"]))[:limit]
            for item in results:
                doc = self.db.execute(
                    "SELECT kind, abstract FROM documents WHERE uri=?", (item["uri"],)
                ).fetchone()
                item.update(dict(doc))
            return results

    def create_session(self, session_id: str, user_id: str):
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO sessions (id, user_id, created_at) VALUES (?, ?, ?)",
                (session_id, user_id, now()),
            )

    def session(self, session_id: str) -> dict:
        with self._lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            result = dict(row)
            result["messages"] = [
                dict(m)
                for m in self.db.execute(
                    "SELECT seq, role, content FROM messages WHERE session_id=? ORDER BY seq",
                    (session_id,),
                )
            ]
            return result

    def append_message(self, session_id: str, role: str, content: str):
        with self._lock, self.db:
            self.session(session_id)
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0)+1 FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?)", (session_id, seq, role, content)
            )
            return seq

    def commit_session(self, snapshot: dict, entries: list[tuple]) -> list[str]:
        with self._lock, self.db:
            current = self.session(snapshot["id"])
            if current["committed_seq"] != snapshot["committed_seq"]:
                raise ConflictError("Session was committed concurrently; retry")
            uris = []
            for doc, chunks, vectors in entries:
                self._put(doc, chunks, vectors)
                uris.append(doc["uri"])
            self.db.execute(
                "UPDATE sessions SET committed_seq=? WHERE id=?",
                (snapshot["messages"][-1]["seq"], snapshot["id"]),
            )
            return uris

    def stats(self) -> dict:
        with self._lock:
            return {
                name: self.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("documents", "chunks", "sessions")
            }
