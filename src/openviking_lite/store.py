# SPDX-License-Identifier: AGPL-3.0
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .models import normalize_vectors


class ConflictError(RuntimeError):
    code = "conflict"


class CommittedMessageError(ConflictError):
    code = "committed_message_changed"


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
            CREATE TABLE IF NOT EXISTS session_links (
                user_id TEXT NOT NULL, external_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                PRIMARY KEY(user_id, external_id)
            );
            CREATE TABLE IF NOT EXISTS message_keys (
                session_id TEXT NOT NULL, message_id TEXT NOT NULL, seq INTEGER NOT NULL,
                PRIMARY KEY(session_id, message_id),
                FOREIGN KEY(session_id, seq) REFERENCES messages(session_id, seq) ON DELETE CASCADE
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

    def put(
        self,
        document: dict,
        chunks: list[tuple[int, str]],
        vectors: np.ndarray,
        *,
        expected_updated_at: str | None = None,
    ):
        with self._lock, self.db:
            if expected_updated_at is not None:
                current = self.db.execute(
                    "SELECT updated_at FROM documents WHERE uri=?", (document["uri"],)
                ).fetchone()
                if current is None or current[0] != expected_updated_at:
                    raise ConflictError(
                        "Document changed since it was opened; reload before saving"
                    )
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
                    "is_doc": "/" not in suffix,
                    "abstract": row["abstract"] if "/" not in suffix else "",
                }
            elif "/" in suffix:
                children[child_uri]["is_dir"] = True
            else:
                children[child_uri]["is_doc"] = True
                children[child_uri]["abstract"] = row["abstract"]
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

    def create_session(self, session_id: str, user_id: str, external_id: str | None = None) -> str:
        with self._lock, self.db:
            if external_id is not None:
                existing = self.db.execute(
                    "SELECT session_id FROM session_links WHERE user_id=? AND external_id=?",
                    (user_id, external_id),
                ).fetchone()
                if existing:
                    return existing[0]
            self.db.execute(
                "INSERT INTO sessions (id, user_id, created_at) VALUES (?, ?, ?)",
                (session_id, user_id, now()),
            )
            if external_id is not None:
                self.db.execute(
                    "INSERT INTO session_links VALUES (?, ?, ?)", (user_id, external_id, session_id)
                )
            return session_id

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

    def list_sessions(self, user_id: str | None, limit: int, offset: int) -> dict:
        clause, args = ("s.user_id=?", (user_id,)) if user_id else ("1=1", ())
        with self._lock:
            total = self.db.execute(
                f"SELECT COUNT(*) FROM sessions s WHERE {clause}", args
            ).fetchone()[0]
            rows = self.db.execute(
                f"""
                SELECT s.*, l.external_id,
                    (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                    (SELECT COUNT(*) FROM messages m
                        WHERE m.session_id=s.id AND m.seq>s.committed_seq) AS pending_messages,
                    (SELECT substr(m.content, 1, 100) FROM messages m
                        WHERE m.session_id=s.id AND m.role='user' ORDER BY m.seq LIMIT 1) AS preview
                FROM sessions s LEFT JOIN session_links l ON l.session_id=s.id
                WHERE {clause} ORDER BY s.created_at DESC, s.id DESC LIMIT ? OFFSET ?
                """,
                (*args, limit, offset),
            ).fetchall()
            return {"sessions": [dict(row) for row in rows], "total": total}

    def append_message(
        self, session_id: str, role: str, content: str, message_id: str | None = None
    ):
        with self._lock, self.db:
            session = self.db.execute(
                "SELECT committed_seq FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(session_id)
            if message_id is not None:
                existing = self.db.execute(
                    """
                    SELECT m.seq, m.role, m.content FROM messages m
                    JOIN message_keys k ON k.session_id=m.session_id AND k.seq=m.seq
                    WHERE k.session_id=? AND k.message_id=?
                """,
                    (session_id, message_id),
                ).fetchone()
                if existing:
                    if existing["role"] == role and existing["content"] == content:
                        return existing["seq"]
                    if existing["seq"] <= session["committed_seq"]:
                        raise CommittedMessageError(
                            "A committed message cannot be changed; use a new message_id"
                        )
                    self.db.execute(
                        "UPDATE messages SET role=?, content=? WHERE session_id=? AND seq=?",
                        (role, content, session_id, existing["seq"]),
                    )
                    return existing["seq"]
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0)+1 FROM messages WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?)", (session_id, seq, role, content)
            )
            if message_id is not None:
                self.db.execute(
                    "INSERT INTO message_keys VALUES (?, ?, ?)", (session_id, message_id, seq)
                )
            return seq

    def commit_session(self, snapshot: dict, entries: list[tuple]) -> list[str]:
        with self._lock, self.db:
            current = self.session(snapshot["id"])
            if current["committed_seq"] != snapshot["committed_seq"]:
                raise ConflictError("Session was committed concurrently; retry")
            original = {m["seq"]: (m["role"], m["content"]) for m in snapshot["messages"]}
            if any(
                original.get(m["seq"]) != (m["role"], m["content"])
                for m in current["messages"]
                if m["seq"] in original
            ):
                raise ConflictError("Session messages changed during memory extraction; retry")
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
            result = {
                name: self.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("documents", "chunks", "sessions")
            }
            result["memories"] = self.db.execute(
                "SELECT COUNT(*) FROM documents WHERE kind='memory'"
            ).fetchone()[0]
            return result
