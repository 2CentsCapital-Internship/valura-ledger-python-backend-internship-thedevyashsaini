from decimal import Decimal

D = Decimal


def leg_amount(prepared, account, side="debit"):
    for leg in prepared.legs:
        if leg.account == account:
            return getattr(leg, side)
    return None


def two_lots(ledger):
    ledger.deposit(amount="10000.00")
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")
    ledger.buy("trd-2", quantity="5", price="20.00", principal="100.00")


def test_a_sale_that_consumes_one_whole_lot(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")

    sold = ledger.sell(
        "trd-2", quantity="10", price="15.00", principal="150.00"
    )

    assert sold.status == "accepted"
    assert leg_amount(sold, "2100") == D("120.00")
    assert ledger.positions() == {}


def test_a_sale_that_consumes_part_of_a_lot(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")

    sold = ledger.sell("trd-2", quantity="4", price="15.00", principal="60.00")

    assert leg_amount(sold, "2100") == D("48.00")
    assert ledger.positions()["ACME"] == {"quantity": "6", "cost_basis": "72.00"}


def test_a_sale_that_crosses_two_lots(ledger):
    two_lots(ledger)

    sold = ledger.sell("trd-3", quantity="12", price="15.00", principal="180.00")

    # 120.00 for all of the first lot, plus 2/5 of the second lot's 100.00.
    assert leg_amount(sold, "2100") == D("160.00")
    assert ledger.positions()["ACME"] == {"quantity": "3", "cost_basis": "60.00"}


def test_partial_cost_relief_uses_the_total_lot_formula(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="3", price="3.3333", principal="10.00")

    sold = ledger.sell("trd-2", quantity="2", price="5.00", principal="10.00")

    # round(10.00 x 2 / 3) is 6.67. A cost per share of 3.33 would give 6.66.
    assert leg_amount(sold, "2100") == D("6.67")
    assert ledger.positions()["ACME"] == {"quantity": "1", "cost_basis": "3.33"}


def test_an_oversell_is_rejected_and_leaves_every_lot_untouched(ledger):
    two_lots(ledger)
    before = [(lot.lot_id, lot.quantity, lot.total_cost) for lot in ledger.lots()]

    rejected = ledger.sell("trd-3", quantity="100", price="15.00", principal="1500.00")

    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "oversell"
    assert rejected.legs == ()
    after = [(lot.lot_id, lot.quantity, lot.total_cost) for lot in ledger.lots()]
    assert after == before
    assert "trd-3" not in ledger.book.trades


def test_fifo_follows_delivery_order_not_price(ledger):
    ledger.deposit()
    # The expensive lot is delivered first, so it is relieved first.
    ledger.buy("trd-1", quantity="5", price="40.00", principal="200.00")
    ledger.buy("trd-2", quantity="5", price="10.00", principal="50.00")

    sold = ledger.sell("trd-3", quantity="5", price="30.00", principal="150.00")

    assert leg_amount(sold, "2100") == D("200.00")
    assert ledger.positions()["ACME"] == {"quantity": "5", "cost_basis": "50.00"}


def test_a_split_scales_quantity_and_preserves_total_cost(ledger):
    two_lots(ledger)

    ledger.send(
        "stock_split",
        {
            "customer_id": "CUST-1",
            "symbol": "ACME",
            "ratio_from": "1",
            "ratio_to": "2",
        },
    )

    assert ledger.positions()["ACME"] == {"quantity": "30", "cost_basis": "220.00"}
    assert [lot.total_cost for lot in ledger.lots()] == [D("120.00"), D("100.00")]


def test_a_split_then_a_sale_relieves_the_scaled_lot_correctly(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")
    ledger.send(
        "stock_split",
        {
            "customer_id": "CUST-1",
            "symbol": "ACME",
            "ratio_from": "1",
            "ratio_to": "2",
        },
    )

    sold = ledger.sell("trd-2", quantity="5", price="8.00", principal="40.00")

    # 5 of 20 shares now, relieving a quarter of the unchanged 120.00 cost.
    assert leg_amount(sold, "2100") == D("30.00")
    assert ledger.positions()["ACME"] == {"quantity": "15", "cost_basis": "90.00"}


def test_a_symbol_change_preserves_lot_order(ledger):
    ledger.deposit()
    ledger.buy("trd-1", symbol="OLD", quantity="4", price="10.00", principal="40.00")
    ledger.buy("trd-2", symbol="NEW", quantity="4", price="25.00", principal="100.00")

    ledger.send(
        "symbol_change",
        {"customer_id": "CUST-1", "old_symbol": "OLD", "new_symbol": "NEW"},
    )

    lots = ledger.lots(symbol="NEW")
    assert [lot.total_cost for lot in lots] == [D("40.00"), D("100.00")]
    assert ledger.lots(symbol="OLD") == []
    assert ledger.positions()["NEW"] == {"quantity": "8", "cost_basis": "140.00"}

    # The renamed lot arrived first, so it is relieved first.
    sold = ledger.sell(
        "trd-3", symbol="NEW", quantity="4", price="30.00", principal="120.00"
    )
    assert leg_amount(sold, "2100") == D("40.00")


def test_a_symbol_change_without_a_position_is_rejected(ledger):
    rejected = ledger.send(
        "symbol_change",
        {"customer_id": "CUST-1", "old_symbol": "OLD", "new_symbol": "NEW"},
    )
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "no_position"


def test_a_reinvested_dividend_adds_a_lot_at_the_net_amount(ledger):
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
    )

    assert ledger.positions()["ACME"] == {"quantity": "12", "cost_basis": "130.00"}
