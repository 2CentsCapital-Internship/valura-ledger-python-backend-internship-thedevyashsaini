import pytest

from book import Book
from engine import LedgerEngine
from storage import Storage


@pytest.fixture()
def engine(tmp_path):
    storage = Storage(tmp_path / "ledger.sqlite3")
    storage.initialize_schema()
    instance = LedgerEngine(storage)
    instance.activate_run("run-1", "practice")
    yield instance
    storage.close()


def deposit(offset, event_id, customer_id="CUST-1", amount="100.00"):
    return {
        "offset": offset,
        "event_id": event_id,
        "type": "deposit",
        "payload": {"customer_id": customer_id, "amount": amount},
    }


def test_persistence_is_atomic_and_advances_the_offset(engine):
    assert engine.process_event(deposit(7, "evt_1")) == "accepted"

    run = engine.storage.get_run("run-1")
    assert run.next_offset == 8
    assert run.last_sequence_no == 1

    stored = engine.storage.get_event("run-1", "evt_1")
    assert stored.status == "accepted"
    assert len(stored.legs) == 2

    postings = engine.pending_postings()
    assert postings == [
        {
            "event_id": "evt_1",
            "legs": [
                {
                    "account": "1100",
                    "customer_id": "CUST-1",
                    "debit": "100.00",
                    "credit": "0.00",
                },
                {
                    "account": "2010",
                    "customer_id": "CUST-1",
                    "debit": "0.00",
                    "credit": "100.00",
                },
            ],
        }
    ]


def test_the_same_event_twice_changes_state_once(engine):
    engine.process_event(deposit(1, "evt_1"))
    before = engine.build_current_checkpoint()

    assert engine.process_event(deposit(1, "evt_1")) == "duplicate"

    assert engine.build_current_checkpoint() == before
    assert engine.counts["duplicate"] == 1
    assert len(engine.pending_postings()) == 1


def test_a_conflicting_duplicate_does_not_replace_the_first_body(engine):
    engine.process_event(deposit(1, "evt_1", amount="100.00"))

    assert engine.process_event(deposit(1, "evt_1", amount="900.00")) == "duplicate"

    assert engine.counts["conflicting_duplicate"] == 1
    snapshot = engine.build_current_checkpoint()
    assert snapshot["customers"]["CUST-1"]["wallet_cash"] == "100.00"

    categories = dict(engine.storage.diagnostics_by_category("run-1"))
    assert categories["conflicting_duplicate"] == 1


def test_stored_event_replay_recreates_an_identical_snapshot(engine):
    engine.process_event(deposit(1, "evt_1", amount="100.00"))
    engine.process_event(deposit(2, "evt_2", "CUST-2", amount="250.50"))
    engine.process_event(
        {
            "offset": 3,
            "event_id": "evt_3",
            "type": "transfer_between_customers",
            "payload": {
                "from_customer_id": "CUST-1",
                "to_customer_id": "CUST-2",
                "amount": "40.00",
            },
        }
    )
    expected = engine.build_current_checkpoint()

    rebuilt = Book()
    for record in engine.storage.load_events("run-1"):
        rebuilt.apply_stored_event(record)

    assert rebuilt.snapshot() == expected


def test_a_rejected_event_stays_one_rejection_and_still_posts_empty_legs(engine):
    bad = deposit(1, "evt_bad", amount="-5.00")

    assert engine.process_event(bad) == "rejected"
    assert engine.process_event(bad) == "duplicate"

    assert engine.counts["rejected"] == 1
    assert engine.pending_postings() == [{"event_id": "evt_bad", "legs": []}]
    assert engine.build_current_checkpoint()["customers"] == {}


def test_the_outbox_holds_one_logical_row_per_event_id(engine):
    engine.process_event(deposit(1, "evt_1"))
    engine.process_event(deposit(1, "evt_1"))
    engine.process_event(deposit(2, "evt_2"))

    assert engine.pending_posting_count() == 2

    engine.acknowledge_postings(["evt_1"])
    assert engine.pending_posting_count() == 1
    assert engine.pending_postings()[0]["event_id"] == "evt_2"


def test_a_restarted_engine_resumes_the_same_run(tmp_path):
    path = tmp_path / "ledger.sqlite3"

    first = Storage(path)
    first.initialize_schema()
    engine_one = LedgerEngine(first)
    engine_one.activate_run("run-1", "practice")
    engine_one.process_event(deposit(1, "evt_1"))
    engine_one.process_event(deposit(2, "evt_2", "CUST-2", "42.00"))
    expected = engine_one.build_current_checkpoint()
    first.close()

    second = Storage(path)
    second.initialize_schema()
    engine_two = LedgerEngine(second)
    engine_two.activate_run("run-1", "practice")

    assert engine_two.build_current_checkpoint() == expected
    assert engine_two.storage.get_active_run("practice").next_offset == 3
    assert engine_two.pending_posting_count() == 2
    second.close()


def test_a_different_run_starts_from_an_empty_book(engine):
    engine.process_event(deposit(1, "evt_1"))
    engine.activate_run("run-2", "practice")

    assert engine.build_current_checkpoint()["customers"] == {}
