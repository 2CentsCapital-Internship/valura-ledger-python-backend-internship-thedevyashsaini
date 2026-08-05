def event(offset, event_id, event_type, payload, **extra):
    body = {
        "offset": offset,
        "event_id": event_id,
        "type": event_type,
        "payload": payload,
    }
    body.update(extra)
    return body


def deposit_event(offset, event_id, customer_id="CUST-1", amount="100.00", **extra):
    return event(
        offset,
        event_id,
        "deposit",
        {"customer_id": customer_id, "amount": amount},
        **extra,
    )


def test_trial_balance_is_debit_positive(ledger):
    ledger.deposit(amount="1000.00")

    trial_balance = ledger.snapshot()["trial_balance"]

    assert trial_balance["1100"] == "1000.00"
    assert trial_balance["2010"] == "-1000.00"
    assert sum(float(v) for v in trial_balance.values()) == 0.0


def test_an_account_that_netted_back_to_zero_is_still_reported(ledger):
    ledger.deposit(amount="100.00", event_id="evt_dep")
    ledger.reverse("evt_dep")

    trial_balance = ledger.snapshot()["trial_balance"]

    assert trial_balance["1100"] == "0.00"
    assert trial_balance["2010"] == "0.00"


def test_wallet_cash_is_reported_credit_positive(ledger):
    ledger.deposit(amount="250.75")
    ledger.send("fee_charged", {"customer_id": "CUST-1", "amount": "0.75"})

    assert ledger.snapshot()["customers"]["CUST-1"]["wallet_cash"] == "250.00"


def test_cash_hold_covers_the_principal_and_the_estimated_charges(ledger):
    ledger.deposit()
    ledger.place(order_id="ord-1", quantity="10", limit_price="12.00",
                 est_charges="2.00")

    assert ledger.snapshot()["customers"]["CUST-1"]["cash_hold"] == "122.00"


def test_a_sell_hold_does_not_count_as_cash_hold(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")
    ledger.place(order_id="ord-2", side="sell", quantity="4", limit_price="15.00")

    assert ledger.snapshot()["customers"]["CUST-1"]["cash_hold"] == "0.00"


def test_a_sell_placement_larger_than_the_position_still_opens_and_routes(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="2", price="12.00", principal="24.00")

    placed = ledger.place(order_id="ord-big", side="sell", quantity="50",
                          limit_price="15.00", asset_class="etf")

    assert placed.status == "accepted"
    assert ledger.snapshot()["open_order_routes"] == {"ord-big": "BRK-A"}


def test_open_order_routes_hold_only_open_orders(ledger):
    ledger.deposit()
    ledger.place(order_id="ord-open", quantity="10", limit_price="12.00")
    ledger.place(order_id="ord-cancel", quantity="10", limit_price="12.00")
    ledger.place(order_id="ord-fill", quantity="10", limit_price="12.00")

    ledger.send("order_cancelled", {"order_id": "ord-cancel"})
    ledger.buy("trd-1", order_id="ord-fill", quantity="10", price="12.00",
               principal="120.00")

    assert ledger.snapshot()["open_order_routes"] == {"ord-open": "BRK-A"}


def test_partial_fills_release_the_hold_without_accumulating_rounding(ledger):
    # 14 * 196.29 + 8.00 = 2756.06, released across two partial fills. Rounding
    # each release separately leaves 393.73; the hold owed on the 2 unfilled
    # shares is 393.72.
    ledger.deposit(amount="50000.00")
    ledger.place(order_id="ord-1", quantity="14", limit_price="196.29",
                 est_charges="8.00")
    ledger.buy("trd-1", quantity="10", price="373.75", principal="3737.50",
               final=False)
    ledger.buy("trd-2", quantity="2", price="192.23", principal="384.46",
               final=False)

    assert ledger.snapshot()["customers"]["CUST-1"]["cash_hold"] == "393.72"


def test_a_closed_order_releases_its_whole_hold(ledger):
    ledger.deposit()
    ledger.place(order_id="ord-1", quantity="10", limit_price="12.00",
                 est_charges="2.00")
    ledger.send("order_cancelled", {"order_id": "ord-1"})

    assert ledger.snapshot()["customers"]["CUST-1"]["cash_hold"] == "0.00"
    assert ledger.book.orders["ord-1"].remaining_cash_hold == 0


def test_position_quantity_and_cost_basis_come_from_the_lots(ledger):
    ledger.deposit()
    ledger.buy("trd-1", quantity="10", price="12.00", principal="120.00")
    ledger.buy("trd-2", quantity="2.5", price="20.00", principal="50.00")

    assert ledger.positions()["ACME"] == {"quantity": "12.5", "cost_basis": "170.00"}


def test_a_customer_named_only_by_a_rejected_event_is_not_reported(ledger):
    ledger.deposit(customer_id="CUST-GHOST", amount="-5.00")

    assert ledger.snapshot()["customers"] == {}


def test_a_transfer_moves_the_wallet_between_two_customers(ledger):
    ledger.deposit(customer_id="CUST-1", amount="100.00")
    ledger.send(
        "transfer_between_customers",
        {
            "from_customer_id": "CUST-1",
            "to_customer_id": "CUST-2",
            "amount": "40.00",
        },
    )

    customers = ledger.snapshot()["customers"]
    assert customers["CUST-1"]["wallet_cash"] == "60.00"
    assert customers["CUST-2"]["wallet_cash"] == "40.00"
    assert ledger.snapshot()["trial_balance"]["2010"] == "-100.00"


def test_an_as_of_checkpoint_excludes_everything_after_the_named_event(engine):
    engine.process_event(deposit_event(1, "evt_1", amount="100.00"))
    engine.process_event(deposit_event(2, "evt_2", amount="50.00"))
    engine.process_event(deposit_event(3, "evt_3", amount="25.00"))

    as_of = engine.build_as_of_checkpoint("evt_2")

    assert as_of["customers"]["CUST-1"]["wallet_cash"] == "150.00"
    assert engine.build_current_checkpoint()["customers"]["CUST-1"][
        "wallet_cash"
    ] == "175.00"


def test_a_later_backdated_event_is_absent_from_an_earlier_as_of_state(engine):
    engine.process_event(deposit_event(1, "evt_1", amount="100.00"))
    engine.process_event(deposit_event(2, "evt_2", amount="50.00"))
    engine.process_event(
        deposit_event(3, "evt_backdated", amount="500.00", backdated_days=9)
    )

    as_of = engine.build_as_of_checkpoint("evt_2")

    assert as_of["customers"]["CUST-1"]["wallet_cash"] == "150.00"


def test_an_as_of_checkpoint_reports_the_lot_book_of_that_moment(engine):
    engine.process_event(deposit_event(1, "evt_dep", amount="10000.00"))
    engine.process_event(
        event(
            2,
            "evt_buy",
            "order_filled",
            {
                "order_id": "ord-1",
                "customer_id": "CUST-1",
                "side": "buy",
                "symbol": "ACME",
                "quantity": "10",
                "price": "12.00",
                "principal": "120.00",
                "asset_class": "equity",
                "broker": "BRK-A",
                "partner_rate": "0.50",
                "trade_id": "trd-1",
            },
        )
    )
    engine.process_event(
        event(
            3,
            "evt_sell",
            "order_filled",
            {
                "order_id": "ord-2",
                "customer_id": "CUST-1",
                "side": "sell",
                "symbol": "ACME",
                "quantity": "4",
                "price": "15.00",
                "principal": "60.00",
                "asset_class": "equity",
                "broker": "BRK-A",
                "partner_rate": "0.50",
                "trade_id": "trd-2",
            },
        )
    )

    as_of = engine.build_as_of_checkpoint("evt_buy")
    assert as_of["customers"]["CUST-1"]["positions"]["ACME"] == {
        "quantity": "10",
        "cost_basis": "120.00",
    }

    current = engine.build_current_checkpoint()
    assert current["customers"]["CUST-1"]["positions"]["ACME"] == {
        "quantity": "6",
        "cost_basis": "72.00",
    }
