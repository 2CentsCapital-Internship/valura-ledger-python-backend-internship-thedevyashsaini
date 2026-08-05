#!/usr/bin/env python3
"""Read-only local inspection of the ledger database.

    uv run python scripts/inspect_db.py runs
    uv run python scripts/inspect_db.py events --run-id RUN_ID
    uv run python scripts/inspect_db.py outbox --run-id RUN_ID
    uv run python scripts/inspect_db.py rejections --run-id RUN_ID
    uv run python scripts/inspect_db.py diagnostics --run-id RUN_ID
    uv run python scripts/inspect_db.py event --run-id RUN_ID --event-id EVT_ID

Nothing here writes to the database, and no credential is ever read or shown.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

TRUNCATE_AT = 400


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def show(value, full: bool) -> str:
    text = json.dumps(value, indent=2, sort_keys=True)
    if full or len(text) <= TRUNCATE_AT:
        return text
    return text[:TRUNCATE_AT] + f"\n  ... ({len(text)} characters, use --full)"


def cmd_runs(connection, args) -> None:
    rows = connection.execute(
        "SELECT * FROM runs ORDER BY started_at"
    ).fetchall()
    for row in rows:
        print(
            f"{row['run_id']}  mode={row['mode']:<10} status={row['status']:<7} "
            f"next_offset={row['next_offset']:<7} events={row['last_sequence_no']:<6} "
            f"started={row['started_at']}"
        )
    if not rows:
        print("no runs recorded")


def cmd_events(connection, args) -> None:
    query = (
        'SELECT sequence_no, "offset", event_id, event_type, status,'
        " rejection_reason FROM events WHERE run_id = ?"
    )
    parameters: list = [args.run_id]
    if args.type:
        query += " AND event_type = ?"
        parameters.append(args.type)
    if args.status:
        query += " AND status = ?"
        parameters.append(args.status)
    query += " ORDER BY sequence_no"

    for row in connection.execute(query, parameters):
        reason = f"  {row['rejection_reason']}" if row["rejection_reason"] else ""
        print(
            f"#{row['sequence_no']:<6} off={row['offset']:<7} "
            f"{row['event_type']:<28} {row['status']:<9} {row['event_id']}{reason}"
        )


def cmd_event(connection, args) -> None:
    row = connection.execute(
        "SELECT e.*, o.status AS outbox_status, o.attempts, o.last_error"
        " FROM events e LEFT JOIN posting_outbox o"
        " ON o.run_id = e.run_id AND o.event_id = e.event_id"
        " WHERE e.run_id = ? AND e.event_id = ?",
        (args.run_id, args.event_id),
    ).fetchone()
    if row is None:
        print("no such event")
        return

    print(f"event_id:   {row['event_id']}")
    print(f"type:       {row['event_type']}")
    print(f"sequence:   {row['sequence_no']}")
    print(f"offset:     {row['offset']}")
    print(f"status:     {row['status']}")
    print(f"rejection:  {row['rejection_reason']}")
    print(f"outbox:     {row['outbox_status']} (attempts {row['attempts']})")
    if row["last_error"]:
        print(f"last error: {row['last_error']}")
    print("\nraw event:\n" + show(json.loads(row["raw_event_json"]), args.full))
    print("\nlegs:\n" + show(json.loads(row["legs_json"]), args.full))
    print("\neffect:\n" + show(json.loads(row["effect_json"]), args.full))


def cmd_outbox(connection, args) -> None:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS total FROM posting_outbox"
        " WHERE run_id = ? GROUP BY status",
        (args.run_id,),
    ).fetchall()
    for row in rows:
        print(f"{row['status']:<14} {row['total']:>6}")

    pending = connection.execute(
        "SELECT event_id, attempts, last_error FROM posting_outbox"
        " WHERE run_id = ? AND status = 'pending' ORDER BY rowid LIMIT 25",
        (args.run_id,),
    ).fetchall()
    if pending:
        print("\npending:")
        for row in pending:
            error = f"  {row['last_error']}" if row["last_error"] else ""
            print(f"  {row['event_id']}  attempts={row['attempts']}{error}")

    checkpoints = connection.execute(
        "SELECT checkpoint_id, status, attempts, as_of_event_id"
        " FROM checkpoint_outbox WHERE run_id = ? ORDER BY rowid",
        (args.run_id,),
    ).fetchall()
    if checkpoints:
        print("\ncheckpoints:")
        for row in checkpoints:
            as_of = f" as_of={row['as_of_event_id']}" if row["as_of_event_id"] else ""
            print(
                f"  {row['checkpoint_id']:<12} {row['status']:<14} "
                f"attempts={row['attempts']}{as_of}"
            )


def cmd_rejections(connection, args) -> None:
    rows = connection.execute(
        "SELECT event_type, rejection_reason, COUNT(*) AS total FROM events"
        " WHERE run_id = ? AND status = 'rejected'"
        " GROUP BY event_type, rejection_reason ORDER BY total DESC",
        (args.run_id,),
    ).fetchall()
    for row in rows:
        print(
            f"{row['event_type']:<30} {str(row['rejection_reason']):<30} "
            f"{row['total']:>5}"
        )
    if not rows:
        print("no rejections")


def cmd_diagnostics(connection, args) -> None:
    if args.category:
        rows = connection.execute(
            "SELECT * FROM diagnostics WHERE run_id = ? AND category = ?"
            " ORDER BY id",
            (args.run_id, args.category),
        ).fetchall()
        for row in rows:
            print(f"#{row['id']} {row['event_id'] or '-'}")
            print(show(json.loads(row["details_json"]), args.full))
        if not rows:
            print("no diagnostics in that category")
        return

    rows = connection.execute(
        "SELECT category, COUNT(*) AS total FROM diagnostics WHERE run_id = ?"
        " GROUP BY category ORDER BY total DESC",
        (args.run_id,),
    ).fetchall()
    for row in rows:
        print(f"{row['category']:<30} {row['total']:>6}")
    if not rows:
        print("no diagnostics")


def main() -> int:
    load_dotenv()
    default_db = os.environ.get("ARENA_DB_PATH", "data/ledger.sqlite3")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=default_db)
    parser.add_argument("--run-id")
    parser.add_argument("--event-id")
    parser.add_argument("--type")
    parser.add_argument("--status")
    parser.add_argument("--category")
    parser.add_argument("--full", action="store_true")
    parser.add_argument(
        "command",
        choices=["runs", "events", "event", "outbox", "rejections", "diagnostics"],
    )
    args = parser.parse_args()

    handlers = {
        "runs": cmd_runs,
        "events": cmd_events,
        "event": cmd_event,
        "outbox": cmd_outbox,
        "rejections": cmd_rejections,
        "diagnostics": cmd_diagnostics,
    }

    if args.command != "runs" and not args.run_id:
        parser.error(f"{args.command} needs --run-id")
    if args.command == "event" and not args.event_id:
        parser.error("event needs --event-id")

    connection = connect(Path(args.db))
    try:
        handlers[args.command](connection, args)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
