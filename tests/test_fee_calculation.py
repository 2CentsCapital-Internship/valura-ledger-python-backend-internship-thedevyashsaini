from decimal import Decimal

import pytest

from book import choose_broker, compute_fill_economics
from models import RejectedEvent

D = Decimal


def test_brokerage_floors_at_the_minimum_fee():
    economics = compute_fill_economics(D("100.00"), "BRK-B", D("0"))
    # 15 bps of 100 is 0.15, below BRK-B's 2.50 minimum.
    assert economics.brokerage == D("2.50")


def test_brokerage_above_the_minimum_is_used_as_calculated():
    economics = compute_fill_economics(D("10000.00"), "BRK-A", D("0"))
    assert economics.brokerage == D("20.00")
    assert economics.custody == D("4.00")


def test_regulatory_fee_is_eight_bps():
    economics = compute_fill_economics(D("10000.00"), "BRK-C", D("0"))
    assert economics.regulatory == D("8.00")


def test_broker_cost_includes_the_ticket_before_rounding():
    economics = compute_fill_economics(D("10000.00"), "BRK-B", D("0"))
    # 8 bps of 10000 is 8.00, plus the 3.00 ticket.
    assert economics.broker_cost == D("11.00")
    assert economics.custody_cost == D("3.00")


def test_partner_share_uses_independently_rounded_amounts():
    economics = compute_fill_economics(D("5000.00"), "BRK-C", D("0.50"))
    margin = economics.brokerage + economics.custody
    margin -= economics.broker_cost + economics.custody_cost
    assert margin == D("7.30")
    assert economics.partner_share == D("3.65")


def test_loss_making_fill_pays_the_partner_nothing():
    economics = compute_fill_economics(D("100.00"), "BRK-B", D("0.50"))
    revenue = economics.brokerage + economics.custody
    cost = economics.broker_cost + economics.custody_cost
    assert cost > revenue
    assert economics.partner_share == D("0.00")


def test_half_cent_partner_share_rounds_away_from_zero():
    # BRK-A on 10000 leaves a 12.65 margin, so a 0.50 rate lands on 6.325.
    economics = compute_fill_economics(D("10000.00"), "BRK-A", D("0.50"))
    margin = economics.brokerage + economics.custody
    margin -= economics.broker_cost + economics.custody_cost
    assert margin == D("12.65")
    assert economics.partner_share == D("6.33")


def test_broker_payable_account_follows_the_broker():
    assert compute_fill_economics(D("500"), "BRK-A", D("0")).broker_payable_account == "2411"
    assert compute_fill_economics(D("500"), "BRK-B", D("0")).broker_payable_account == "2412"
    assert compute_fill_economics(D("500"), "BRK-C", D("0")).broker_payable_account == "2413"


def test_routing_picks_the_cheapest_total_customer_charge():
    # equity: BRK-A charges 24 bps, BRK-B charges 20 bps.
    assert choose_broker("equity", D("100"), D("100")) == "BRK-B"
    # bond: BRK-B charges 20 bps, BRK-C charges 28 bps.
    assert choose_broker("bond", D("100"), D("100")) == "BRK-B"
    # etf: BRK-A charges 24 bps, BRK-C charges 28 bps.
    assert choose_broker("etf", D("100"), D("100")) == "BRK-A"


def test_routing_tie_breaks_on_broker_id_ascending():
    # At 1315.79 principal BRK-A charges 2.63 + 0.53 and BRK-B, still on its
    # 2.50 floor, charges 2.50 + 0.66. Both total 3.16.
    assert choose_broker("equity", D("1"), D("1315.79")) == "BRK-A"


def test_routing_rejects_an_asset_class_no_broker_trades():
    with pytest.raises(RejectedEvent):
        choose_broker("crypto", D("100"), D("100"))
