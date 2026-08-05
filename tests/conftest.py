"""Shared test driver.

Every event is pushed through the same path production uses: prepare, persist
(here, serialise through the exact JSON the store would write), then apply the
stored record. A serialisation bug in a reversible effect therefore fails a
unit test rather than a restart.
"""
import json
import itertools

import pytest

from book import Book
from engine import LedgerEngine
from models import JournalLeg, StoredEvent
from storage import Storage, canonical_json


class Driver:
    """Drives a bare :class:`Book`, without SQLite."""

    def __init__(self) -> None:
        self.book = Book()
        self.sequence = 0
        self._ids = itertools.count(1)

    def send(self, event_type: str, payload: dict, event_id: str | None = None):
        self.sequence += 1
        event_id = event_id or f"evt_{next(self._ids)}"
        event = {
            "offset": self.sequence,
            "event_id": event_id,
            "type": event_type,
            "payload": payload,
        }

        prepared = self.book.prepare_event(event, self.sequence)
        stored = StoredEvent(
            run_id="run-test",
            sequence_no=self.sequence,
            offset=self.sequence,
            event_id=event_id,
            event_type=event_type,
            raw_event=event,
            content_hash="",
            status=prepared.status,
            rejection_reason=prepared.rejection_reason,
            legs=tuple(
                JournalLeg.from_dict(leg.to_payload()) for leg in prepared.legs
            ),
            effect=json.loads(canonical_json(prepared.effect)),
        )
        self.book.apply_stored_event(stored)
        return prepared

    # -- convenience builders ---------------------------------------------
    def deposit(self, customer_id="CUST-1", amount="10000.00", event_id=None):
        return self.send(
            "deposit",
            {"customer_id": customer_id, "amount": amount},
            event_id,
        )

    def place(
        self,
        order_id="ord-1",
        customer_id="CUST-1",
        side="buy",
        symbol="ACME",
        quantity="10",
        limit_price="12.00",
        asset_class="equity",
        est_charges="2.00",
        event_id=None,
    ):
        return self.send(
            "order_placed",
            {
                "order_id": order_id,
                "customer_id": customer_id,
                "side": side,
                "symbol": symbol,
                "quantity": quantity,
                "limit_price": limit_price,
                "asset_class": asset_class,
                "est_charges": est_charges,
            },
            event_id,
        )

    def fill(
        self,
        trade_id,
        order_id="ord-1",
        customer_id="CUST-1",
        side="buy",
        symbol="ACME",
        quantity="10",
        price="12.00",
        principal="120.00",
        asset_class="equity",
        broker="BRK-A",
        partner_rate="0.50",
        final=True,
        event_id=None,
    ):
        return self.send(
            "order_filled" if final else "order_partially_filled",
            {
                "order_id": order_id,
                "customer_id": customer_id,
                "side": side,
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "principal": principal,
                "asset_class": asset_class,
                "broker": broker,
                "partner_rate": partner_rate,
                "trade_id": trade_id,
            },
            event_id,
        )

    def buy(self, trade_id, **kwargs):
        return self.fill(trade_id, side="buy", **kwargs)

    def sell(self, trade_id, **kwargs):
        kwargs.setdefault("order_id", "ord-sell")
        return self.fill(trade_id, side="sell", **kwargs)

    def reverse(self, target_event_id, event_id=None):
        return self.send(
            "reversal",
            {"reverses_event_id": target_event_id, "reason": "operator error"},
            event_id,
        )

    def snapshot(self):
        return self.book.snapshot()

    def positions(self, customer_id="CUST-1"):
        return self.snapshot()["customers"].get(customer_id, {}).get("positions", {})

    def lots(self, customer_id="CUST-1", symbol="ACME"):
        return self.book.lots.get((customer_id, symbol), [])


@pytest.fixture()
def ledger():
    return Driver()


@pytest.fixture()
def engine(tmp_path):
    storage = Storage(tmp_path / "ledger.sqlite3")
    storage.initialize_schema()
    instance = LedgerEngine(storage)
    instance.activate_run("run-1", "practice")
    yield instance
    storage.close()
