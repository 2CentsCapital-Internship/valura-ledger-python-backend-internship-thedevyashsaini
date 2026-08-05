# Valura Ledger Arena — double-entry ledger consumer

A single-process Python program that subscribes to the arena's event feed,
keeps a double-entry book of record, submits the journal legs each event
produced, and answers current and historical checkpoints.

There is no inbound server here. Nothing needs a public address, a domain, a
certificate or a cloud account: the program makes outbound HTTPS requests to
the arena and everything else is local.

## Source of truth

The live protocol endpoint at `/protocol` is canonical. The `PROTOCOL.md` in
this repository is the copy that shipped with the starter kit and may be stale;
where the two disagree, believe the live task sheet and `/v1/rules`.

## Setup

```bash
uv sync
cp .env.example .env
```

Then put your key in the local `.env`. It is the only place it should ever
exist: `.env` is ignored by Git, never logged, and never written to SQLite.

## Configuration

| Variable | Meaning |
| --- | --- |
| `ARENA_BASE_URL` | Arena base URL. Must be `https://`. |
| `ARENA_API_KEY` | Your key from the portal. Local only. |
| `ARENA_DB_PATH` | SQLite file. Defaults to `data/ledger.sqlite3`. |
| `ARENA_BATCH_SIZE` | Postings buffered before a flush is triggered (1–500). |
| `ARENA_FLUSH_MS` | Milliseconds before an idle flush is triggered. |
| `ARENA_MAX_SECONDS` | Local safety deadline. Defaults per mode. |
| `ARENA_LOG_LEVEL` | Python log level. |

Every one of these can be overridden on the command line (`--url`, `--db`,
`--batch-size`, `--flush-ms`, `--max-seconds`, `--log-level`). Command line
beats environment beats default.

## Running

Practice:

```bash
uv run python client.py --mode practice
```

Submission:

```bash
uv run python client.py --mode submission --new-run
```

Resume an interrupted submission — no `--new-run`, so a dropped connection can
never spend a second attempt:

```bash
uv run python client.py --mode submission
```

Final:

```bash
uv run python client.py --mode final --new-run
```

Resume an interrupted final:

```bash
uv run python client.py --mode final
```

Status, which consumes no attempt:

```bash
uv run python client.py --status --mode practice
uv run python client.py --status --mode submission
uv run python client.py --status --mode final
```

Tests:

```bash
uv run pytest
```

Local database inspection, read-only:

```bash
uv run python scripts/inspect_db.py runs
uv run python scripts/inspect_db.py events --run-id RUN_ID
uv run python scripts/inspect_db.py outbox --run-id RUN_ID
uv run python scripts/inspect_db.py rejections --run-id RUN_ID
uv run python scripts/inspect_db.py diagnostics --run-id RUN_ID
uv run python scripts/inspect_db.py event --run-id RUN_ID --event-id EVT_ID
```

## Architecture

```
arena SSE stream -> client.py -> engine.py -> storage.py (SQLite)
                                           -> book.py    (in-memory projection)
                                           -> posting outbox -> POST /v1/postings
checkpoint_request -> current or as-of snapshot -> POST /v1/checkpoint
```

* **`client.py`** is transport only. It parses SSE frames, reconnects, drains
  the outbox, and answers checkpoints. It holds no accounting state.
* **`engine.py`** owns ordering, first-delivery-wins deduplication, atomic
  persistence, and historical replay.
* **`storage.py`** is the only module that executes SQL. It stores runs, the
  first-seen body and content hash of every event, the legs and reversible
  effect each produced, the posting and checkpoint outboxes, and diagnostics.
* **`book.py`** is the ledger: chart of accounts, tariffs, handlers, the lot
  book, and the checkpoint snapshot.

`Book.prepare_event` validates and calculates without mutating anything;
`Book.apply_stored_event` is the only mutating path and it works from what was
persisted. That split is what makes a restart, a stream rewind, and an as-of
query all reproduce exactly the book that was originally committed.

## Accounting conventions

* `Decimal` everywhere. No binary floating point touches money, a rate, a
  quantity or a cost basis.
* Money is rounded to the cent half away from zero, and every derived amount
  (brokerage, custody, regulatory fee, broker cost, custody cost, partner
  share) is rounded independently before use.
* Balances are debit-positive: assets and expenses are normally positive,
  liabilities and income normally negative.
* Balances are keyed by `(customer_id, account)`, never by account alone. A
  transfer between two customers lands on 2010 twice and would otherwise
  vanish.
* Revenue and cost are booked gross. The regulatory fee is a liability, not
  income; broker cost includes the flat ticket fee; the partner share is zero
  when cost exceeds revenue.
* FIFO consumes lots in delivery order, not trade date. A partial consumption
  relieves `round(lot_total × sold_qty / lot_qty)` and the remainder stays with
  the lot.
* Quantities are plain decimal strings with no exponent notation.

## Recovery behaviour

* The resume offset is durable. `runs.next_offset` advances inside the same
  transaction that inserts an event and its outbox row, so the two can never
  disagree.
* On startup the book is rebuilt by replaying the run's committed events.
* A redelivered `event_id` is ignored; a redelivered `event_id` with a
  different body is recorded as a conflicting duplicate and the first body
  stays canonical.
* Postings live in a SQLite outbox and are acknowledged only after a 2xx, so
  an HTTP failure or a crash resends rather than loses them. 429 responses
  respect `Retry-After`.
* A checkpoint payload is persisted before it is sent and reused verbatim on
  retry, so a late answer still describes the checkpoint's place in the stream.
* An interrupted scarce attempt is resumed by rerunning without `--new-run`.
  The client refuses to send `new=true` while an unfinished local run exists.

## Known limitations

* Soft invariants (a fill whose principal disagrees with quantity × price, a
  dividend whose gross minus tax disagrees with the net, an FX deposit whose
  converted amounts disagree with amount × rate, a fill routed to a broker
  other than the calculated route) are logged as diagnostics rather than
  rejected. They are the candidates for the feed's systematic defect;
  promoting one to a rejection is a decision practice feedback should make,
  not a guess.
* As-of checkpoints replay the run's stored events from the beginning rather
  than from a periodic snapshot. At 6,000 events that is comfortably inside
  the response window, so the snapshot optimisation is deliberately not built.
* A split rounds each scaled lot to the six decimal places quantities carry,
  half away from zero, rather than keeping the exact quotient. The protocol
  states the scaling rule and the six-place limit but not the rounding rule
  between them; rounding per lot is what keeps a position equal to the sum of
  its lots.
