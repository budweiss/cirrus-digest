"""Private, host-owned conversation checkpoints. No sends, inference or sync."""
from __future__ import annotations
import hashlib
import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:20]


class Memory:
    def __init__(self, root):
        self.directory = Path(root) / "data/conversations"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "memory.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, client TEXT NOT NULL,
                project TEXT NOT NULL, thread TEXT NOT NULL, question TEXT NOT NULL,
                privacy TEXT NOT NULL, state TEXT NOT NULL, answer TEXT NOT NULL,
                meta TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS conversation_scope ON turns(client, project, thread, created)")
        os.chmod(self.directory, 0o700)
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def row(row):
        if row is None:
            return None
        result = dict(row)
        result["meta"] = json.loads(result["meta"])
        return result

    def add(self, *, owner, client, project, thread, message_id, question,
            privacy="LOCAL_ONLY", state="queued", meta=None):
        if privacy not in ("LOCAL_ONLY", "CLOUD_ALLOWED"):
            raise ValueError("unknown privacy")
        if not all(isinstance(x, str) and x for x in (owner, client, project, thread, message_id, question)):
            raise ValueError("missing request identity or text")
        key = identity(owner, client, project, message_id)
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (key, owner, client, project, thread, question[:12000], privacy,
                        state, "", json.dumps(meta or {}), now, now))
        return self.get(key)

    def get(self, key):
        with self.connect() as db:
            return self.row(db.execute("SELECT * FROM turns WHERE id=?", (key,)).fetchone())

    def update(self, key, *, state=None, answer=None, meta=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db.execute("SELECT * FROM turns WHERE id=?", (key,)).fetchone())
            if row is None:
                raise ValueError("unknown request")
            if meta:
                row["meta"].update(meta)
            db.execute("UPDATE turns SET state=?,answer=?,meta=?,updated=? WHERE id=?",
                       (state or row["state"], row["answer"] if answer is None else answer,
                        json.dumps(row["meta"]), time.time(), key))
        return self.get(key)

    def claim(self, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db.execute(
                "SELECT * FROM turns WHERE owner=? AND state='queued' ORDER BY created,id LIMIT 1",
                (owner,)).fetchone())
            if row:
                db.execute("UPDATE turns SET state='running',updated=? WHERE id=?", (time.time(), row["id"]))
        return self.get(row["id"]) if row else None

    def transition(self, key, expected, state):
        with self.connect() as db:
            changed = db.execute("UPDATE turns SET state=?,updated=? WHERE id=? AND state=?",
                                 (state, time.time(), key, expected)).rowcount
        return bool(changed)

    def history(self, row, *, same_thread=True, limit=8):
        query = "SELECT * FROM turns WHERE owner=? AND client=? AND project=? AND id<>?"
        args = [row["owner"], row["client"], row["project"], row["id"]]
        if same_thread:
            query += " AND thread=?"
            args.append(row["thread"])
        query += " AND created<=? ORDER BY created DESC,id DESC LIMIT ?"
        args.extend([row["created"], limit])
        with self.connect() as db:
            return list(reversed([self.row(x) for x in db.execute(query, args)]))

    def recent(self, owner, client, limit=5):
        with self.connect() as db:
            return [self.row(x) for x in db.execute(
                "SELECT * FROM turns WHERE owner=? AND client=? ORDER BY created DESC LIMIT ?",
                (owner, client, limit))]

    def pending_notices(self, owner):
        with self.connect() as db:
            rows = [self.row(x) for x in db.execute(
                "SELECT * FROM turns WHERE owner=? AND state IN ('ready','needs_review','delivery_unknown')",
                (owner,))]
        return [row for row in rows if not row["meta"].get("notice_attempted")]

    def recover(self, owner):
        """Call only while holding the worker lock; never assume an in-flight call failed."""
        with self.connect() as db:
            rows = [self.row(x) for x in db.execute(
                "SELECT * FROM turns WHERE owner=? AND state IN ('running','sending')", (owner,))]
        for row in rows:
            unknown = row["state"] == "sending" or bool(row["meta"].get("active_stage"))
            state = "delivery_unknown" if row["state"] == "sending" else ("needs_review" if unknown else "queued")
            self.update(row["id"], state=state, meta={
                "reason": "Interrupted during an unconfirmed action; review before retry." if unknown
                          else "Resuming from saved checkpoints.",
                "recovered": True})
            if unknown:
                self.review_packet(self.get(row["id"]))
        return len(rows)

    def review_packet(self, row):
        with self.connect() as db:
            first = db.execute("SELECT question FROM turns WHERE owner=? AND client=? AND project=? AND thread=? ORDER BY created,id LIMIT 1",
                               (row["owner"], row["client"], row["project"], row["thread"])).fetchone()
        packet = {"original_question": first["question"], "request": row, "history": self.history(row),
                  "instruction": "Review locally. Delivery or further model attempts are held."}
        path = self.directory / (row["id"] + "-review.json")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(packet, stream, ensure_ascii=False, indent=2)
        return path


def selftest():
    import unittest
    from test_answer_workflow import MemoryTests
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(MemoryTests))
    return result.wasSuccessful()


if __name__ == "__main__":
    import sys
    sys.exit(0 if len(sys.argv) == 2 and sys.argv[1] == "--selftest" and selftest() else 1)
