"""All SQLite access. No other module executes SQL.

SQLite is the durable source of truth: the first-seen body of every event, the
legs and reversible effect it produced, the posting and checkpoint outboxes,
and the resume offset. The in-memory book is only a projection of what is
committed here, so a crash costs at most the event that was in flight.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from models import JournalLeg, PreparedEvent, RunRecord, StoredEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,

    next_offset INTEGER NOT NULL DEFAULT 0,
    last_sequence_no INTEGER NOT NULL DEFAULT 0,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    "offset" INTEGER NOT NULL,

    event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,

    raw_event_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,

    status TEXT NOT NULL,
    rejection_reason TEXT,

    legs_json TEXT NOT NULL,
    effect_json TEXT NOT NULL,

    received_at TEXT NOT NULL,

    PRIMARY KEY (run_id, event_id),
    UNIQUE (run_id, sequence_no),

    FOREIGN KEY (run_id)
        REFERENCES runs(run_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_run_sequence
ON events(run_id, sequence_no);

CREATE INDEX IF NOT EXISTS idx_events_run_offset
ON events(run_id, "offset");

CREATE INDEX IF NOT EXISTS idx_events_run_type
ON events(run_id, event_type);

CREATE TABLE IF NOT EXISTS posting_outbox (
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,

    posting_json TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,

    created_at TEXT NOT NULL,
    acknowledged_at TEXT,

    PRIMARY KEY (run_id, event_id),

    FOREIGN KEY (run_id, event_id)
        REFERENCES events(run_id, event_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_posting_outbox_pending
ON posting_outbox(run_id, status);

CREATE TABLE IF NOT EXISTS checkpoint_outbox (
    run_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,

    requested_sequence_no INTEGER NOT NULL,
    as_of_event_id TEXT,

    payload_json TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,

    created_at TEXT NOT NULL,
    acknowledged_at TEXT,

    PRIMARY KEY (run_id, checkpoint_id),

    FOREIGN KEY (run_id)
        REFERENCES runs(run_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    run_id TEXT,
    event_id TEXT,

    category TEXT NOT NULL,
    details_json TEXT NOT NULL,

    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_run_category
ON diagnostics(run_id, category);
"""

DIAGNOSTIC_CATEGORIES = {
    "conflicting_duplicate",
    "malformed_envelope",
    "rejected_event",
    "hard_invariant_failure",
    "soft_invariant_warning",
    "posting_http_error",
    "posting_response",
    "checkpoint_http_error",
    "checkpoint_response",
    "stream_parse_error",
    "stream_reset",
    "unexpected_exception",
}


def canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_hash(value: dict) -> str:
    encoded = canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(
            self.db_path,
            timeout=5,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row

        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")

        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def initialize_schema(self) -> None:
        self.connection.executescript(SCHEMA)

    # -- runs --------------------------------------------------------------
    @staticmethod
    def _run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            mode=row["mode"],
            status=row["status"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            next_offset=row["next_offset"],
            last_sequence_no=row["last_sequence_no"],
        )

    def get_active_run(self, mode: str) -> RunRecord | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE mode = ? AND status = 'active'"
            " ORDER BY started_at DESC LIMIT 1",
            (mode,),
        ).fetchone()
        return self._run_record(row) if row else None

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return self._run_record(row) if row else None

    def start_or_resume_run(self, run_id: str, mode: str) -> RunRecord:
        existing = self.get_run(run_id)
        if existing is not None:
            return existing

        now = _now()
        with self.transaction():
            self.connection.execute(
                "INSERT INTO runs (run_id, mode, status, started_at, ended_at,"
                " next_offset, last_sequence_no, created_at, updated_at)"
                " VALUES (?, ?, 'active', ?, NULL, 0, 0, ?, ?)",
                (run_id, mode, now, now, now),
            )
        return self.get_run(run_id)

    def mark_run_ended(self, run_id: str) -> None:
        now = _now()
        with self.transaction():
            self.connection.execute(
                "UPDATE runs SET status = 'ended', ended_at = ?, updated_at = ?"
                " WHERE run_id = ?",
                (now, now, run_id),
            )

    # -- events ------------------------------------------------------------
    @staticmethod
    def _stored_event(row: sqlite3.Row) -> StoredEvent:
        return StoredEvent(
            run_id=row["run_id"],
            sequence_no=row["sequence_no"],
            offset=row["offset"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            raw_event=json.loads(row["raw_event_json"]),
            content_hash=row["content_hash"],
            status=row["status"],
            rejection_reason=row["rejection_reason"],
            legs=tuple(
                JournalLeg.from_dict(item) for item in json.loads(row["legs_json"])
            ),
            effect=json.loads(row["effect_json"]),
        )

    def get_event(self, run_id: str, event_id: str) -> StoredEvent | None:
        row = self.connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND event_id = ?",
            (run_id, event_id),
        ).fetchone()
        return self._stored_event(row) if row else None

    def load_events(
        self,
        run_id: str,
        through_sequence: int | None = None,
    ) -> list[StoredEvent]:
        if through_sequence is None:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence_no",
                (run_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND sequence_no <= ?"
                " ORDER BY sequence_no",
                (run_id, through_sequence),
            ).fetchall()
        return [self._stored_event(row) for row in rows]

    def get_event_sequence(self, run_id: str, event_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT sequence_no FROM events WHERE run_id = ? AND event_id = ?",
            (run_id, event_id),
        ).fetchone()
        return row["sequence_no"] if row else None

    def persist_event_and_outbox(
        self,
        run_id: str,
        prepared_event: PreparedEvent,
        raw_event: dict,
        event_hash: str,
    ) -> StoredEvent:
        legs_payload = [leg.to_payload() for leg in prepared_event.legs]
        posting = {"event_id": prepared_event.event_id, "legs": legs_payload}
        now = _now()

        with self.transaction():
            row = self.connection.execute(
                "SELECT next_offset, last_sequence_no FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"run {run_id} is not registered")

            sequence_no = row["last_sequence_no"] + 1
            next_offset = max(row["next_offset"], prepared_event.offset + 1)

            self.connection.execute(
                'INSERT INTO events (run_id, sequence_no, "offset", event_id,'
                " event_type, raw_event_json, content_hash, status,"
                " rejection_reason, legs_json, effect_json, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    sequence_no,
                    prepared_event.offset,
                    prepared_event.event_id,
                    prepared_event.event_type,
                    canonical_json(raw_event),
                    event_hash,
                    prepared_event.status,
                    prepared_event.rejection_reason,
                    json.dumps(legs_payload, separators=(",", ":")),
                    canonical_json(prepared_event.effect),
                    now,
                ),
            )
            self.connection.execute(
                "INSERT INTO posting_outbox (run_id, event_id, posting_json,"
                " status, attempts, created_at)"
                " VALUES (?, ?, ?, 'pending', 0, ?)",
                (
                    run_id,
                    prepared_event.event_id,
                    json.dumps(posting, separators=(",", ":")),
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE runs SET next_offset = ?, last_sequence_no = ?,"
                " updated_at = ? WHERE run_id = ?",
                (next_offset, sequence_no, now, run_id),
            )

        return StoredEvent(
            run_id=run_id,
            sequence_no=sequence_no,
            offset=prepared_event.offset,
            event_id=prepared_event.event_id,
            event_type=prepared_event.event_type,
            raw_event=raw_event,
            content_hash=event_hash,
            status=prepared_event.status,
            rejection_reason=prepared_event.rejection_reason,
            legs=prepared_event.legs,
            effect=prepared_event.effect,
        )

    # -- posting outbox ----------------------------------------------------
    def get_pending_postings(self, run_id: str, limit: int = 500) -> list[dict]:
        rows = self.connection.execute(
            "SELECT event_id, posting_json FROM posting_outbox"
            " WHERE run_id = ? AND status = 'pending'"
            " ORDER BY rowid LIMIT ?",
            (run_id, limit),
        ).fetchall()
        return [json.loads(row["posting_json"]) for row in rows]

    def count_pending_postings(self, run_id: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS total FROM posting_outbox"
            " WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        ).fetchone()
        return row["total"]

    def mark_postings_acknowledged(self, run_id: str, event_ids: list[str]) -> None:
        if not event_ids:
            return

        now = _now()
        with self.transaction():
            self.connection.executemany(
                "UPDATE posting_outbox SET status = 'acknowledged',"
                " acknowledged_at = ? WHERE run_id = ? AND event_id = ?",
                [(now, run_id, event_id) for event_id in event_ids],
            )

    def record_posting_failure(
        self,
        run_id: str,
        event_ids: list[str],
        error: str,
    ) -> None:
        if not event_ids:
            return

        with self.transaction():
            self.connection.executemany(
                "UPDATE posting_outbox SET attempts = attempts + 1,"
                " last_error = ? WHERE run_id = ? AND event_id = ?",
                [(error[:500], run_id, event_id) for event_id in event_ids],
            )

    # -- checkpoint outbox -------------------------------------------------
    def save_checkpoint(
        self,
        run_id: str,
        checkpoint_id: str,
        sequence_no: int,
        as_of_event_id: str | None,
        payload: dict,
    ) -> None:
        now = _now()
        with self.transaction():
            self.connection.execute(
                "INSERT OR IGNORE INTO checkpoint_outbox (run_id, checkpoint_id,"
                " requested_sequence_no, as_of_event_id, payload_json, status,"
                " attempts, created_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)",
                (
                    run_id,
                    checkpoint_id,
                    sequence_no,
                    as_of_event_id,
                    canonical_json(payload),
                    now,
                ),
            )

    def get_pending_checkpoint(self, run_id: str, checkpoint_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT payload_json, status FROM checkpoint_outbox"
            " WHERE run_id = ? AND checkpoint_id = ?",
            (run_id, checkpoint_id),
        ).fetchone()
        if row is None or row["status"] != "pending":
            return None
        return json.loads(row["payload_json"])

    def get_pending_checkpoints(self, run_id: str) -> list[tuple[str, dict]]:
        rows = self.connection.execute(
            "SELECT checkpoint_id, payload_json FROM checkpoint_outbox"
            " WHERE run_id = ? AND status = 'pending' ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [(row["checkpoint_id"], json.loads(row["payload_json"])) for row in rows]

    def mark_checkpoint_acknowledged(self, run_id: str, checkpoint_id: str) -> None:
        now = _now()
        with self.transaction():
            self.connection.execute(
                "UPDATE checkpoint_outbox SET status = 'acknowledged',"
                " acknowledged_at = ? WHERE run_id = ? AND checkpoint_id = ?",
                (now, run_id, checkpoint_id),
            )

    def record_checkpoint_failure(
        self,
        run_id: str,
        checkpoint_id: str,
        error: str,
    ) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE checkpoint_outbox SET attempts = attempts + 1,"
                " last_error = ? WHERE run_id = ? AND checkpoint_id = ?",
                (error[:500], run_id, checkpoint_id),
            )

    # -- diagnostics -------------------------------------------------------
    def add_diagnostic(
        self,
        category: str,
        details: dict,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO diagnostics (run_id, event_id, category,"
                " details_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, event_id, category, canonical_json(details), _now()),
            )

    def diagnostics_by_category(self, run_id: str) -> list[tuple[str, int]]:
        rows = self.connection.execute(
            "SELECT category, COUNT(*) AS total FROM diagnostics"
            " WHERE run_id = ? GROUP BY category ORDER BY total DESC",
            (run_id,),
        ).fetchall()
        return [(row["category"], row["total"]) for row in rows]

    def load_diagnostics(
        self,
        run_id: str,
        category: str | None = None,
    ) -> list[dict]:
        if category is None:
            rows = self.connection.execute(
                "SELECT * FROM diagnostics WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM diagnostics WHERE run_id = ? AND category = ?"
                " ORDER BY id",
                (run_id, category),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event_id": row["event_id"],
                "category": row["category"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def rejection_counts(self, run_id: str) -> list[tuple[str, str, int]]:
        rows = self.connection.execute(
            "SELECT event_type, rejection_reason, COUNT(*) AS total FROM events"
            " WHERE run_id = ? AND status = 'rejected'"
            " GROUP BY event_type, rejection_reason ORDER BY total DESC",
            (run_id,),
        ).fetchall()
        return [
            (row["event_type"], row["rejection_reason"] or "", row["total"])
            for row in rows
        ]
