from decimal import Decimal

D = Decimal


def test_a_reversed_deposit_returns_the_wallet_to_zero(ledger):
    ledger.deposit(amount="100.00", event_id="evt_dep")
    assert ledger.snapshot()["customers"]["CUST-1"]["wallet_cash"] == "100.00"

    reversal = ledger.reverse("evt_dep")

    assert reversal.status == "accepted"
    assert {(leg.account, leg.debit, leg.credit) for leg in reversal.legs} == {
        ("1100", D("0"), D("100.00")),
        ("2010", D("100.00"), D("0")),
    }
    assert ledger.snapshot()["customers"]["CUST-1"]["wallet_cash"] == "0.00"
    assert ledger.book.trial_balance_total() == D("0.00")


def test_a_reversed_buy_removes_its_lot_and_its_trade(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00",
               event_id="evt_buy")
    assert ledger.positions()["ACME"]["quantity"] == "10"

    ledger.reverse("evt_buy")

    assert ledger.positions() == {}
    assert "trd-1" not in ledger.book.trades


def test_a_reversed_sell_restores_the_exact_lots(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")
    ledger.buy("trd-2", quantity="5", price="20.00", principal="100.00")
    before = [(lot.lot_id, lot.quantity, lot.total_cost) for lot in ledger.lots()]

    ledger.sell("trd-3", quantity="12", price="15.00", principal="180.00",
                event_id="evt_sell")
    assert ledger.positions()["ACME"] == {"quantity": "3", "cost_basis": "60.00"}

    ledger.reverse("evt_sell")

    assert [(lot.lot_id, lot.quantity, lot.total_cost) for lot in ledger.lots()] == before
    assert ledger.positions()["ACME"] == {"quantity": "15", "cost_basis": "220.00"}


def test_reversing_a_fill_does_not_restore_the_hold_or_reopen_the_order(ledger):
    ledger.deposit()
    ledger.place(order_id="ord-1", quantity="10", limit_price="12.00",
                 est_charges="2.00")
    assert ledger.snapshot()["customers"]["CUST-1"]["cash_hold"] == "122.00"

    ledger.fill("trd-1", order_id="ord-1", quantity="4", price="12.00",
                principal="48.00", final=False, event_id="evt_fill")
    assert ledger.snapshot()["customers"]["CUST-1"]["cash_hold"] == "73.20"

    ledger.reverse("evt_fill")

    snapshot = ledger.snapshot()
    assert snapshot["customers"]["CUST-1"]["cash_hold"] == "73.20"
    assert snapshot["open_order_routes"] == {"ord-1": "BRK-A"}
    assert ledger.positions() == {}


def test_reversing_a_final_fill_leaves_the_order_closed(ledger):
    ledger.deposit()
    ledger.place(order_id="ord-1", quantity="10", limit_price="12.00")
    ledger.buy("trd-1", order_id="ord-1", quantity="10", price="12.00",
               principal="120.00", event_id="evt_fill")
    assert ledger.snapshot()["open_order_routes"] == {}

    ledger.reverse("evt_fill")

    snapshot = ledger.snapshot()
    assert snapshot["open_order_routes"] == {}
    assert snapshot["customers"]["CUST-1"]["cash_hold"] == "0.00"


def test_a_reversed_reinvested_dividend_removes_its_lot(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")
    ledger.send(
        "dividend_reinvested",
        {
            "customer_id": "CUST-1",
            "symbol": "ACME",
            "gross_amount": "12.00",
            "withholding_tax": "2.00",
            "net_amount": "10.00",
            "reinvest_price": "5.00",
            "reinvest_quantity": "2",
        },
        event_id="evt_drip",
    )
    assert ledger.positions()["ACME"] == {"quantity": "12", "cost_basis": "130.00"}

    ledger.reverse("evt_drip")

    assert ledger.positions()["ACME"] == {"quantity": "10", "cost_basis": "120.00"}


def test_a_reversed_split_restores_the_original_quantities(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")
    ledger.send(
        "stock_split",
        {
            "customer_id": "CUST-1",
            "symbol": "ACME",
            "ratio_from": "1",
            "ratio_to": "3",
        },
        event_id="evt_split",
    )
    assert ledger.positions()["ACME"]["quantity"] == "30"

    ledger.reverse("evt_split")

    assert ledger.positions()["ACME"] == {"quantity": "10", "cost_basis": "120.00"}


def test_a_reversed_symbol_change_restores_the_old_symbol(ledger):
    ledger.deposit()
    ledger.buy("trd-1", symbol="OLD", quantity="4", price="10.00", principal="40.00")
    ledger.send(
        "symbol_change",
        {"customer_id": "CUST-1", "old_symbol": "OLD", "new_symbol": "NEW"},
        event_id="evt_rename",
    )
    assert set(ledger.positions()) == {"NEW"}

    ledger.reverse("evt_rename")

    assert set(ledger.positions()) == {"OLD"}
    assert ledger.positions()["OLD"] == {"quantity": "4", "cost_basis": "40.00"}


def test_a_reversed_fee_refund_makes_the_fee_refundable_again(ledger):
    ledger.deposit()
    ledger.send("fee_charged", {"customer_id": "CUST-1", "amount": "5.00"},
                event_id="evt_fee")
    ledger.send(
        "fee_refund",
        {"refunds_source_id": "evt_fee", "customer_id": "CUST-1"},
        event_id="evt_refund",
    )
    assert ledger.book.fee_charges["evt_fee"].refunded is True

    ledger.reverse("evt_refund")

    assert ledger.book.fee_charges["evt_fee"].refunded is False
    again = ledger.send(
        "fee_refund", {"refunds_source_id": "evt_fee", "customer_id": "CUST-1"}
    )
    assert again.status == "accepted"


def test_a_reversed_withdrawal_settlement_returns_it_to_requested(ledger):
    ledger.deposit()
    ledger.send(
        "withdrawal_requested",
        {"withdrawal_id": "wd-1", "customer_id": "CUST-1", "amount": "50.00"},
    )
    ledger.send("withdrawal_settled", {"withdrawal_id": "wd-1"}, event_id="evt_settle")
    assert ledger.book.withdrawals["wd-1"].status == "settled"

    ledger.reverse("evt_settle")

    assert ledger.book.withdrawals["wd-1"].status == "requested"


def test_a_reversed_trade_settlement_returns_it_to_unsettled(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")
    ledger.send("trade_settled", {"trade_id": "trd-1"}, event_id="evt_settle")
    assert ledger.book.trades["trd-1"].status == "settled"

    ledger.reverse("evt_settle")

    assert ledger.book.trades["trd-1"].status == "unsettled"


def test_a_reversal_of_an_event_never_received_is_rejected(ledger):
    rejected = ledger.reverse("evt_never_seen")

    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "unknown_reversal_target"
    assert rejected.legs == ()


def test_a_reversal_of_a_rejected_event_is_rejected(ledger):
    ledger.deposit(amount="-1.00", event_id="evt_bad")

    rejected = ledger.reverse("evt_bad")

    assert rejected.rejection_reason == "reversal_target_rejected"


def test_reversing_the_same_event_twice_is_rejected(ledger):
    ledger.deposit(amount="100.00", event_id="evt_dep")
    ledger.reverse("evt_dep", event_id="evt_rev")

    second = ledger.reverse("evt_dep")

    assert second.rejection_reason == "already_reversed"


def test_reversing_a_reversal_reapplies_the_original(ledger):
    ledger.deposit(amount="100.00", event_id="evt_dep")
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00",
               event_id="evt_buy")
    ledger.reverse("evt_buy", event_id="evt_rev")
    assert ledger.positions() == {}

    ledger.reverse("evt_rev")

    assert ledger.positions()["ACME"] == {"quantity": "10", "cost_basis": "120.00"}
    assert "evt_buy" not in ledger.book.reversed_event_ids


def test_an_event_cannot_reverse_itself(ledger):
    rejected = ledger.send(
        "reversal",
        {"reverses_event_id": "evt_self", "reason": "loop"},
        event_id="evt_self",
    )
    assert rejected.rejection_reason == "reversal_cycle"
