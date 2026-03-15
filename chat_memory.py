#!/opt/homebrew/bin/python3.14
"""
Semantic chat memory for the OpenClaw iMessage bridge.

Storage layout per chat:
  ~/.openclaw/memory/<guid_safe>/
    chunks.json      -- [{text, sender, timestamp, chat_guid}]
    embeddings.json  -- [[float, ...], ...]  (parallel array to chunks)
    state.json       -- {last_indexed_rowid, built_at}

Chunking: 5-message sliding windows, step 2 (50% overlap).
Embeddings: OpenAI text-embedding-3-small via urllib (no extra deps).
Search: pure Python cosine similarity.
"""

import json
import math
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone

_DEFAULT_MEMORY_DIR = os.path.expanduser("~/.openclaw/memory")
_EMBED_MODEL = "text-embedding-3-small"
_WINDOW = 5
_STEP = 2
_BATCH_SIZE = 100


class ChatMemory:
    def __init__(self, guid: str, data_dir: str, api_key: str) -> None:
        self.guid = guid
        self.api_key = api_key
        safe = guid.replace(";", "_").replace("+", "").replace(" ", "_")
        self.storage_dir = os.path.join(data_dir, safe)
        self.chunks: list[dict] = []
        self.embeddings: list[list[float]] = []
        self._state: dict = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def load_or_build(
        cls,
        guid: str,
        conn: sqlite3.Connection,
        api_key: str,
        data_dir: str = _DEFAULT_MEMORY_DIR,
    ) -> "ChatMemory":
        mem = cls(guid, data_dir, api_key)
        os.makedirs(mem.storage_dir, exist_ok=True)
        mem._load()
        last_rowid = mem._state.get("last_indexed_rowid", 0)
        new_msgs = mem._fetch_messages(conn, last_rowid)
        if new_msgs:
            mem._index_messages(new_msgs)
        return mem

    def update(self, new_messages: list[dict]) -> None:
        """Index new messages (already fetched from DB) into memory."""
        if not new_messages:
            return
        last_rowid = self._state.get("last_indexed_rowid", 0)
        to_index = [m for m in new_messages if m.get("ROWID", 0) > last_rowid]
        if to_index:
            self._index_messages(to_index)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Return top_k most relevant chunks for the query."""
        if not self.chunks or not self.api_key:
            return []
        query_vecs = self._embed_texts([query])
        if not query_vecs or not query_vecs[0]:
            return []
        query_vec = query_vecs[0]
        scored = []
        for chunk, emb in zip(self.chunks, self.embeddings):
            if not emb:
                continue
            score = self._cosine(query_vec, emb)
            scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored[:top_k]]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_messages(self, conn: sqlite3.Connection, last_rowid: int) -> list[dict]:
        query = """
            SELECT m.ROWID, m.text, m.is_from_me, m.date,
                   COALESCE(h.id, 'unknown') AS sender
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE c.guid = ?
              AND m.ROWID > ?
              AND m.text IS NOT NULL
            ORDER BY m.ROWID ASC
        """
        cur = conn.execute(query, (self.guid, last_rowid))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _make_chunks(self, messages: list[dict]) -> list[dict]:
        if not messages:
            return []
        chunks = []
        total = len(messages)
        if total < _WINDOW:
            # Small batch: one chunk from all messages
            lines = self._format_lines(messages)
            if lines:
                mid = messages[total // 2]
                chunks.append(self._make_chunk_entry(lines, mid))
            return chunks
        i = 0
        while i + _WINDOW <= total:
            window_msgs = messages[i:i + _WINDOW]
            lines = self._format_lines(window_msgs)
            if lines:
                mid = window_msgs[_WINDOW // 2]
                chunks.append(self._make_chunk_entry(lines, mid))
            i += _STEP
        return chunks

    def _format_lines(self, msgs: list[dict]) -> list[str]:
        lines = []
        for msg in msgs:
            sender = "me" if msg.get("is_from_me") else msg.get("sender", "unknown")
            text = (msg.get("text") or "").strip()
            if text:
                lines.append(f"{sender}: {text}")
        return lines

    def _make_chunk_entry(self, lines: list[str], mid_msg: dict) -> dict:
        return {
            "text": "\n".join(lines),
            "sender": mid_msg.get("sender", "unknown"),
            "timestamp": mid_msg.get("date", 0),
            "chat_guid": self.guid,
        }

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i:i + _BATCH_SIZE]
            payload = json.dumps({"model": _EMBED_MODEL, "input": batch}).encode()
            req = urllib.request.Request(
                "https://api.openai.com/v1/embeddings",
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode())
                sorted_items = sorted(data["data"], key=lambda x: x["index"])
                all_embeddings.extend(item["embedding"] for item in sorted_items)
            except Exception:
                all_embeddings.extend([] for _ in batch)
        return all_embeddings

    def _index_messages(self, messages: list[dict]) -> None:
        new_chunks = self._make_chunks(messages)
        if not new_chunks:
            return
        new_embeddings = self._embed_texts([c["text"] for c in new_chunks])
        self.chunks.extend(new_chunks)
        self.embeddings.extend(new_embeddings)
        self._state["last_indexed_rowid"] = messages[-1]["ROWID"]
        self._state["built_at"] = datetime.now(tz=timezone.utc).isoformat()
        self._save()

    def _load(self) -> None:
        for attr, filename in (
            ("chunks", "chunks.json"),
            ("embeddings", "embeddings.json"),
            ("_state", "state.json"),
        ):
            path = os.path.join(self.storage_dir, filename)
            try:
                with open(path) as f:
                    setattr(self, attr, json.load(f))
            except (FileNotFoundError, json.JSONDecodeError):
                setattr(self, attr, {} if attr == "_state" else [])

    def _save(self) -> None:
        for attr, filename in (
            ("chunks", "chunks.json"),
            ("embeddings", "embeddings.json"),
            ("_state", "state.json"),
        ):
            path = os.path.join(self.storage_dir, filename)
            with open(path, "w") as f:
                json.dump(getattr(self, attr), f)

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)
