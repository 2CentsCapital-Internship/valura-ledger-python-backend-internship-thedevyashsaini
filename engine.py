"""Coordinates the book, the store, deduplication, and the outboxes.

The transport never touches the book directly. It hands whole events to
``process_event`` and asks for checkpoint payloads; everything about ordering,
first-delivery-wins, durability, and replay lives here.
"""
from __future__ import annotations

from book import Book
from models import StoredEvent
from storage import Storage, event_content_hash

CONTROL_EVENT_TYPES = {"stream_open", "stream_reset", "stream_end"}


class EngineError(RuntimeError):
    """A failure that must stop consumption rather than be worked around."""


class LedgerEngine:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.book = Book()
        self.run_id: str | None = None
        self.mode: str | None = None

        self.counts = {
            "accepted": 0,
            "rejected": 0,
            "duplicate": 0,
            "conflicting_duplicate": 0,
            "malformed": 0,
        }

    # -- runs --------------------------------------------------------------
    def activate_run(self, run_id: str, mode: str) -> None:
        self.storage.start_or_resume_run(run_id, mode)

        if self.run_id == run_id:
            self.mode = mode
            return

        self.book = Book()
        for record in self.storage.load_events(run_id):
            self.book.apply_stored_event(record)

        self.run_id = run_id
        self.mode = mode

    def mark_run_ended(self) -> None:
        if self.run_id is not None:
            self.storage.mark_run_ended(self.run_id)

    def _require_run(self) -> str:
        if self.run_id is None:
            raise EngineError("no active run: stream_open has not been seen")
        return self.run_id

    # -- events ------------------------------------------------------------
    @staticmethod
    def _envelope_problem(event: dict) -> str | None:
        offset = event.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return "invalid_offset"

        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            return "invalid_type"

        if not isinstance(event.get("payload"), dict):
            return "invalid_payload"

        return None

    def process_event(self, event: dict) -> str:
        run_id = self._require_run()

        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            self.counts["malformed"] += 1
            self.storage.add_diagnostic(
                "malformed_envelope",
                {"reason": "missing_event_id", "keys": sorted(event.keys())},
                run_id=run_id,
            )
            return "malformed"

        event_hash = event_content_hash(event)

        existing = self.storage.get_event(run_id, event_id)
        if existing is not None:
            if existing.content_hash != event_hash:
                self.counts["conflicting_duplicate"] += 1
                self.storage.add_diagnostic(
                    "conflicting_duplicate",
                    {
                        "event_type": existing.event_type,
                        "first_hash": existing.content_hash,
                        "later_hash": event_hash,
                        "first_offset": existing.offset,
                        "later_offset": event.get("offset"),
                    },
                    run_id=run_id,
                    event_id=event_id,
                )
            else:
                self.counts["duplicate"] += 1
            return "duplicate"

        problem = self._envelope_problem(event)
        if problem is not None:
            self.counts["malformed"] += 1
            self.storage.add_diagnostic(
                "malformed_envelope",
                {"reason": problem, "event_type": str(event.get("type"))},
                run_id=run_id,
                event_id=event_id,
            )
            normalized = dict(event)
            normalized["offset"] = (
                event["offset"]
                if isinstance(event.get("offset"), int)
                and not isinstance(event.get("offset"), bool)
                and event["offset"] >= 0
                else 0
            )
            normalized["type"] = (
                event["type"] if isinstance(event.get("type"), str) else "unknown"
            )
            normalized["payload"] = {}
            prepared = self.book.prepare_event(normalized, 0)
        else:
            sequence_no = self.storage.get_run(run_id).last_sequence_no + 1
            prepared = self.book.prepare_event(event, sequence_no)
            self._record_warnings(run_id, event_id, event["type"])

        stored = self.storage.persist_event_and_outbox(
            run_id, prepared, event, event_hash
        )

        try:
            self.book.apply_stored_event(stored)
        except Exception as exc:
            self.storage.add_diagnostic(
                "hard_invariant_failure",
                {"event_type": stored.event_type, "error": repr(exc)[:400]},
                run_id=run_id,
                event_id=event_id,
            )
            raise EngineError(
                f"failed to apply committed event {event_id}: {exc!r}"
            ) from exc

        if prepared.status == "rejected":
            self.counts["rejected"] += 1
            self.storage.add_diagnostic(
                "rejected_event",
                {
                    "event_type": prepared.event_type,
                    "reason": prepared.rejection_reason,
                },
                run_id=run_id,
                event_id=event_id,
            )
        else:
            self.counts["accepted"] += 1

        return prepared.status

    def _record_warnings(self, run_id: str, event_id: str, event_type: str) -> None:
        for warning in self.book.warnings:
            details = dict(warning)
            details.setdefault("event_type", event_type)
            self.storage.add_diagnostic(
                "soft_invariant_warning",
                details,
                run_id=run_id,
                event_id=event_id,
            )
        self.book.warnings = []

    # -- checkpoints -------------------------------------------------------
    def build_current_checkpoint(self) -> dict:
        return self.book.snapshot()

    def build_as_of_checkpoint(self, event_id: str) -> dict:
        run_id = self._require_run()

        sequence_no = self.storage.get_event_sequence(run_id, event_id)
        if sequence_no is None:
            return self.book.snapshot()

        records: list[StoredEvent] = self.storage.load_events(
            run_id, through_sequence=sequence_no
        )
        return Book.snapshot_as_of(records)

    def current_sequence_no(self) -> int:
        run_id = self._require_run()
        run = self.storage.get_run(run_id)
        return run.last_sequence_no if run else 0

    def save_checkpoint(
        self,
        checkpoint_id: str,
        as_of_event_id: str | None,
        payload: dict,
    ) -> None:
        run_id = self._require_run()
        self.storage.save_checkpoint(
            run_id,
            checkpoint_id,
            self.current_sequence_no(),
            as_of_event_id,
            payload,
        )

    # -- outbox ------------------------------------------------------------
    def pending_postings(self, limit: int = 500) -> list[dict]:
        run_id = self._require_run()
        return self.storage.get_pending_postings(run_id, limit)

    def pending_posting_count(self) -> int:
        run_id = self._require_run()
        return self.storage.count_pending_postings(run_id)

    def acknowledge_postings(self, event_ids: list[str]) -> None:
        run_id = self._require_run()
        self.storage.mark_postings_acknowledged(run_id, event_ids)

    def record_posting_failure(self, event_ids: list[str], error: str) -> None:
        run_id = self._require_run()
        self.storage.record_posting_failure(run_id, event_ids, error)
