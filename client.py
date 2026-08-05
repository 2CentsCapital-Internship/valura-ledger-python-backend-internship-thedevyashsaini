#!/usr/bin/env python3
"""Transport: the SSE subscription, the posting outbox drain, and checkpoints.

This is the only module that talks to the arena. It owns no accounting state:
every ledger event goes to :class:`LedgerEngine`, and every posting it sends
comes out of the SQLite outbox rather than an in-memory queue, so a crash or a
reconnect loses nothing that was already committed.

    uv run python client.py --mode practice
    uv run python client.py --mode submission --new-run

The live task sheet is the specification. Read it first.
"""
from __future__ import annotations

import collections
import json
import logging
import sys
import time

import httpx

from config import ConfigError, Options, Settings, load_settings
from decimal_utils import ZERO, money
from engine import EngineError, LedgerEngine
from storage import Storage

log = logging.getLogger("arena")

MAX_POSTING_BATCH = 500
CHECKPOINT_ATTEMPTS = 4
DRAIN_ROUNDS = 40


class ArenaClient:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        engine: LedgerEngine,
        new_run: bool,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.engine = engine

        self.send_new_run = new_run
        self.cursor = 0
        self.done = False

        self.stats = {
            "events": 0,
            "posted": 0,
            "checkpoints": 0,
            "reconnects": 0,
            "resets": 0,
            "errors": 0,
        }
        self.mismatches_by_event_type: collections.Counter = collections.Counter()
        self.mismatches_by_account: collections.Counter = collections.Counter()
        self.checkpoint_feedback: list[dict] = []

    # -- helpers -----------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.settings.base_url}{path}"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            headers={"Authorization": f"Bearer {self.settings.api_key}"}
        )

    @staticmethod
    def _retry_after(response: httpx.Response, default: float = 5.0) -> float:
        try:
            return float(response.headers.get("Retry-After", default))
        except (TypeError, ValueError):
            return default

    # -- preflight ---------------------------------------------------------
    def preflight(self, http: httpx.Client) -> None:
        print(f"mode: {self.settings.mode}")

        try:
            rules = http.get(self._url("/v1/rules"), timeout=20).json()
            print("rules:", json.dumps(rules))
        except (httpx.HTTPError, ValueError) as exc:
            print(f"rules unavailable ({type(exc).__name__})")

        try:
            me = http.get(
                self._url("/v1/me"),
                params={"mode": self.settings.mode},
                timeout=20,
            ).json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"standings unavailable ({type(exc).__name__})")
            return

        self._print_me(me)

    @staticmethod
    def _print_me(me: dict) -> None:
        if not isinstance(me, dict):
            return

        best = {
            key: me.get(key)
            for key in ("run_id", "score", "attempts", "attempts_remaining",
                        "runs_remaining", "seconds_remaining")
            if me.get(key) is not None
        }
        if best:
            print("best run:", json.dumps(best))
        for name, value in (me.get("breakdown") or {}).items():
            if isinstance(value, dict):
                print(f"  {name:<28} {value.get('points')} / {value.get('max')}")

        latest = me.get("latest_run")
        if isinstance(latest, dict):
            print("latest run:", json.dumps(
                {k: v for k, v in latest.items() if k != "breakdown"}
            ))
            for name, value in (latest.get("breakdown") or {}).items():
                if isinstance(value, dict):
                    print(f"  {name:<28} {value.get('points')} / {value.get('max')}")

    def confirm_new_attempt(self) -> bool:
        mode = self.settings.mode
        if mode == "practice":
            return True

        if mode == "final":
            print("\n  You are about to consume the only FINAL attempt.")
        else:
            print("\n  You are about to consume one SUBMISSION attempt.")
        print("  It cannot be undone and the score for this tier depends on it.")

        if input(f"  Type: {mode}\n  > ").strip() != mode:
            print("  cancelled.")
            return False
        if input("  Type: START\n  > ").strip() != "START":
            print("  cancelled.")
            return False
        return True

    # -- postings ----------------------------------------------------------
    def flush(self, http: httpx.Client) -> bool:
        """Send at most one batch from the durable outbox.

        Returns True while rows remain, so callers can keep draining. Rows are
        acknowledged only after a 2xx: an HTTP failure leaves them pending.
        """
        postings = self.engine.pending_postings(MAX_POSTING_BATCH)
        if not postings:
            return False

        event_ids = [item["event_id"] for item in postings]

        try:
            response = http.post(
                self._url("/v1/postings"),
                params={"mode": self.settings.mode},
                json={"postings": postings},
                timeout=30,
            )
            if response.status_code == 429:
                time.sleep(self._retry_after(response))
                return True

            response.raise_for_status()
            self.engine.acknowledge_postings(event_ids)
            self.stats["posted"] += len(postings)

            if self.settings.mode == "practice":
                self._review_posting_response(response)
            return True

        except httpx.HTTPError as exc:
            self.stats["errors"] += 1
            self.engine.record_posting_failure(event_ids, repr(exc))
            self.storage.add_diagnostic(
                "posting_http_error",
                {"error": type(exc).__name__, "batch": len(postings)},
                run_id=self.engine.run_id,
            )
            time.sleep(1)
            return True

    def drain(self, http: httpx.Client, rounds: int = DRAIN_ROUNDS) -> None:
        for _ in range(rounds):
            if self.engine.pending_posting_count() == 0:
                return
            self.flush(http)

        remaining = self.engine.pending_posting_count()
        if remaining:
            log.error("giving up with %d postings still pending", remaining)

    def _review_posting_response(self, response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            return

        self.storage.add_diagnostic(
            "posting_response",
            body if isinstance(body, dict) else {"body": body},
            run_id=self.engine.run_id,
        )

        for item in self._result_items(body):
            event_id = item.get("event_id")
            if not event_id or item.get("duplicate"):
                continue

            correct = item.get("correct")
            if correct is None:
                correct = item.get("ok", item.get("is_correct"))
            if correct is not False:
                continue

            stored = self.storage.get_event(self.engine.run_id, event_id)
            event_type = stored.event_type if stored else "unknown"
            accounts = item.get("disagreeing_accounts") or item.get("accounts") or []
            if not isinstance(accounts, list):
                accounts = [str(accounts)]

            self.mismatches_by_event_type[event_type] += 1
            for account in accounts:
                self.mismatches_by_account[str(account)] += 1

            print(
                "practice mismatch:"
                f"\n  event_id: {event_id}"
                f"\n  event_type: {event_type}"
                f"\n  balanced: {item.get('balanced')}"
                f"\n  disagreeing_accounts: {', '.join(str(a) for a in accounts)}",
                flush=True,
            )

    @staticmethod
    def _result_items(body) -> list[dict]:
        if isinstance(body, list):
            return [item for item in body if isinstance(item, dict)]
        if not isinstance(body, dict):
            return []
        for key in ("results", "postings", "events", "details", "feedback"):
            value = body.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    # -- checkpoints -------------------------------------------------------
    def handle_checkpoint_request(self, http: httpx.Client, event: dict) -> None:
        payload = event.get("payload") or {}
        checkpoint_id = payload.get("checkpoint_id")
        if not checkpoint_id or self.engine.run_id is None:
            self.storage.add_diagnostic(
                "malformed_envelope",
                {"reason": "checkpoint_without_id"},
                run_id=self.engine.run_id,
            )
            return

        as_of_event_id = payload.get("as_of_event_id")

        body = self.storage.get_checkpoint_payload(self.engine.run_id, checkpoint_id)
        if body is None:
            if as_of_event_id:
                snapshot = self.engine.build_as_of_checkpoint(as_of_event_id)
            else:
                snapshot = self.engine.build_current_checkpoint()

            self._check_trial_balance(snapshot, checkpoint_id)
            body = {"checkpoint_id": checkpoint_id, **snapshot}
            self.engine.save_checkpoint(checkpoint_id, as_of_event_id, body)

        self.send_checkpoint(http, checkpoint_id, body)

    def _check_trial_balance(self, snapshot: dict, checkpoint_id: str) -> None:
        total = money(
            sum(
                (money(value) for value in snapshot["trial_balance"].values()),
                ZERO,
            )
        )
        if total != ZERO:
            log.error("trial balance for %s totals %s, not zero", checkpoint_id, total)
            self.storage.add_diagnostic(
                "hard_invariant_failure",
                {"reason": "trial_balance_nonzero", "total": str(total)},
                run_id=self.engine.run_id,
            )

    def send_checkpoint(
        self,
        http: httpx.Client,
        checkpoint_id: str,
        body: dict,
    ) -> None:
        for attempt in range(CHECKPOINT_ATTEMPTS):
            try:
                response = http.post(
                    self._url("/v1/checkpoint"),
                    params={"mode": self.settings.mode},
                    json=body,
                    timeout=30,
                )
                if response.status_code == 429:
                    time.sleep(self._retry_after(response))
                    continue

                response.raise_for_status()
                self.storage.mark_checkpoint_acknowledged(
                    self.engine.run_id, checkpoint_id
                )
                self.stats["checkpoints"] += 1

                try:
                    feedback = response.json()
                except ValueError:
                    feedback = {}
                if isinstance(feedback, dict) and feedback:
                    self.storage.add_diagnostic(
                        "checkpoint_response",
                        feedback,
                        run_id=self.engine.run_id,
                    )
                    if self.settings.mode == "practice":
                        self.checkpoint_feedback.append(
                            {"checkpoint_id": checkpoint_id, **feedback}
                        )
                        print(f"checkpoint {checkpoint_id}: {json.dumps(feedback)}",
                              flush=True)
                return

            except httpx.HTTPError as exc:
                self.stats["errors"] += 1
                self.storage.record_checkpoint_failure(
                    self.engine.run_id, checkpoint_id, repr(exc)
                )
                self.storage.add_diagnostic(
                    "checkpoint_http_error",
                    {"checkpoint_id": checkpoint_id, "error": type(exc).__name__},
                    run_id=self.engine.run_id,
                )
                time.sleep(min(2 ** attempt, 8))

        log.error("checkpoint %s was not delivered", checkpoint_id)

    def resend_pending_checkpoints(self, http: httpx.Client) -> None:
        if self.engine.run_id is None:
            return
        for checkpoint_id, body in self.storage.get_pending_checkpoints(
            self.engine.run_id
        ):
            self.send_checkpoint(http, checkpoint_id, body)

    # -- stream ------------------------------------------------------------
    def _stream_params(self) -> dict:
        params = {"mode": self.settings.mode, "from": self.cursor}
        if self.send_new_run:
            params["new"] = "true"
        return params

    def consume(self, http: httpx.Client, deadline: float) -> None:
        last_flush = time.time()
        parse_errors = 0

        with http.stream(
            "GET",
            self._url("/v1/stream"),
            params=self._stream_params(),
            timeout=httpx.Timeout(None, connect=20),
        ) as response:
            response.raise_for_status()

            event_type: str | None = None
            data_lines: list[str] = []

            for line in response.iter_lines():
                if time.time() > deadline:
                    log.warning("local safety deadline reached")
                    return

                if line.startswith(":"):
                    continue

                if line != "":
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        chunk = line[len("data:"):]
                        data_lines.append(chunk[1:] if chunk.startswith(" ") else chunk)
                    continue

                if not data_lines:
                    event_type = None
                    continue

                raw = "\n".join(data_lines)
                frame_name, event_type, data_lines = event_type, None, []

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    parse_errors += 1
                    self.storage.add_diagnostic(
                        "stream_parse_error",
                        {"length": len(raw), "consecutive": parse_errors},
                        run_id=self.engine.run_id,
                    )
                    if parse_errors >= 3:
                        continue
                    return

                parse_errors = 0
                if not isinstance(event, dict):
                    continue

                if self.dispatch(http, event, frame_name):
                    return

                if (
                    self.engine.pending_posting_count() >= self.settings.batch_size
                    or (time.time() - last_flush) * 1000 > self.settings.flush_ms
                ):
                    self.flush(http)
                    last_flush = time.time()

    def dispatch(
        self,
        http: httpx.Client,
        event: dict,
        frame_name: str | None = None,
    ) -> bool:
        """Handle one frame. Returns True when the stream must be reopened.

        Control frames are named by the SSE ``event:`` line and need not repeat
        that name in their data, so both sources are consulted.
        """
        names = {name for name in (frame_name, event.get("type")) if name}

        if "stream_open" in names:
            self.on_stream_open(http, event)
            return False

        if "stream_reset" in names:
            self.on_stream_reset(http, event)
            return True

        if "stream_end" in names:
            self.on_stream_end(http)
            return True

        offset = event.get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool):
            self.cursor = max(self.cursor, offset + 1)

        if "checkpoint_request" in names:
            self.handle_checkpoint_request(http, event)
            self.flush(http)
            return False

        if self.engine.run_id is None:
            # A ledger event before any stream_open: reopen rather than guess
            # which run it belongs to.
            log.warning("ledger event before stream_open; reconnecting")
            self.storage.add_diagnostic(
                "malformed_envelope", {"reason": "event_before_stream_open"}
            )
            return True

        self.engine.process_event(event)
        self.stats["events"] += 1
        return False

    def on_stream_open(self, http: httpx.Client, event: dict) -> None:
        payload = event.get("payload") or {}
        run_id = event.get("run_id") or payload.get("run_id")
        resumed_from = event.get("resumed_from", payload.get("resumed_from"))
        next_event_in = event.get(
            "next_event_in_seconds", payload.get("next_event_in_seconds")
        )

        if not run_id:
            log.error("stream_open carried no run id")
            return

        self.engine.activate_run(run_id, self.settings.mode)
        self.send_new_run = False

        run = self.storage.get_run(run_id)
        if run is not None:
            self.cursor = max(self.cursor, run.next_offset)

        print(
            f"  connected: run={run_id}, resumed_from={resumed_from}, "
            f"next_event_in={next_event_in}s",
            flush=True,
        )
        self.resend_pending_checkpoints(http)

    def on_stream_reset(self, http: httpx.Client, event: dict) -> None:
        payload = event.get("payload") or event
        resume_from = payload.get("resume_from", event.get("resume_from"))

        if isinstance(resume_from, int):
            self.cursor = resume_from

        self.stats["resets"] += 1
        self.storage.add_diagnostic(
            "stream_reset",
            {"resume_from": resume_from},
            run_id=self.engine.run_id,
        )
        self.flush(http)

    def on_stream_end(self, http: httpx.Client) -> None:
        self.drain(http)
        self.resend_pending_checkpoints(http)
        self.engine.mark_run_ended()
        self.done = True

    # -- lifecycle ---------------------------------------------------------
    def run(self) -> dict:
        deadline = time.time() + self.settings.max_seconds

        with self._client() as http:
            while time.time() < deadline and not self.done:
                try:
                    self.consume(http, deadline)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 409:
                        print("  the arena reports the last run already finished; "
                              "start a new attempt deliberately with --new-run")
                        break
                    self.stats["reconnects"] += 1
                    print(f"  reconnecting after HTTP {exc.response.status_code}",
                          flush=True)
                    time.sleep(2)
                except httpx.HTTPError as exc:
                    self.stats["reconnects"] += 1
                    print(f"  reconnecting after {type(exc).__name__}", flush=True)
                    time.sleep(1)

            if self.engine.run_id is not None:
                self.drain(http)

            try:
                me = http.get(
                    self._url("/v1/me"),
                    params={"mode": self.settings.mode},
                    timeout=20,
                ).json()
            except (httpx.HTTPError, ValueError):
                me = {}

        return {"stats": self.stats, "me": me}

    def report(self, outcome: dict) -> None:
        print("\nstats:", json.dumps(outcome["stats"]))
        print("events:", json.dumps(self.engine.counts))

        if self.engine.run_id is not None:
            pending = self.engine.pending_posting_count()
            print(f"pending postings: {pending}")

            rejections = self.storage.rejection_counts(self.engine.run_id)
            if rejections:
                print("\nrejections by reason:")
                for event_type, reason, total in rejections:
                    print(f"  {event_type:<28} {reason:<28} {total:>5}")

            warnings = collections.Counter(
                entry["details"].get("code", "unknown")
                for entry in self.storage.load_diagnostics(
                    self.engine.run_id, "soft_invariant_warning"
                )
            )
            if warnings:
                print("\nsoft invariant warnings:")
                for code, total in warnings.most_common():
                    print(f"  {code:<40} {total:>5}")

            categories = dict(
                self.storage.diagnostics_by_category(self.engine.run_id)
            )
            if categories.get("conflicting_duplicate"):
                print(f"\nconflicting duplicates: "
                      f"{categories['conflicting_duplicate']}")

        if self.mismatches_by_event_type:
            print("\nmismatches by event type:")
            for event_type, total in self.mismatches_by_event_type.most_common():
                print(f"  {event_type:<28} {total:>5}")
        if self.mismatches_by_account:
            print("\nmismatches by account:")
            for account, total in self.mismatches_by_account.most_common():
                print(f"  {account:<28} {total:>5}")

        me = outcome.get("me") or {}
        if me:
            print()
            self._print_me(me)
        else:
            print("\nscore: withheld on this tier")


def main(argv: list[str] | None = None) -> int:
    try:
        settings, options = load_settings(argv)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return 2

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    storage = Storage(settings.db_path)
    storage.initialize_schema()
    engine = LedgerEngine(storage)

    try:
        return _run(settings, options, storage, engine)
    finally:
        storage.close()


def _run(
    settings: Settings,
    options: Options,
    storage: Storage,
    engine: LedgerEngine,
) -> int:
    active = storage.get_active_run(settings.mode)

    if options.status:
        client = ArenaClient(settings, storage, engine, new_run=False)
        with client._client() as http:
            client.preflight(http)
        if active is not None:
            print(f"local active run: {active.run_id} "
                  f"(next offset {active.next_offset})")
        else:
            print("local active run: none")
        return 0

    new_run = options.new_run

    if settings.mode == "practice":
        new_run = False
    elif active is not None:
        if options.new_run:
            print("  an unfinished local run exists for this mode; resuming it "
                  "instead of spending another attempt.")
        new_run = False
    elif not options.new_run:
        print(f"  no unfinished {settings.mode} run exists locally.")
        print(f"  starting one costs an attempt: rerun with --new-run.")
        return 1

    client = ArenaClient(settings, storage, engine, new_run=new_run)

    if active is not None:
        client.cursor = active.next_offset
        engine.activate_run(active.run_id, settings.mode)
        print(f"resuming run {active.run_id} from offset {active.next_offset}")

    with client._client() as http:
        if settings.mode != "practice":
            client.preflight(http)
            if new_run and not client.confirm_new_attempt():
                return 1

    print(f"connecting to {settings.base_url} as {settings.mode} ...", flush=True)

    try:
        outcome = client.run()
    except EngineError as exc:
        log.error("stopping: %s", exc)
        print("\nthe ledger could not apply a committed event. Restart to rebuild "
              "from SQLite once the cause is fixed.")
        return 3

    client.report(outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
