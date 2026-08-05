from decimal import Decimal

import pytest

from decimal_utils import bps_money, decimal_value, money, money_str, quantity, quantity_str


def test_money_rounds_half_away_from_zero():
    assert money("1.005") == Decimal("1.01")
    assert money("-1.005") == Decimal("-1.01")
    assert money("2.675") == Decimal("2.68")


def test_money_str_always_has_two_places():
    assert money_str("10") == "10.00"
    assert money_str(Decimal("-0.005")) == "-0.01"
    assert money_str("0") == "0.00"


def test_quantity_str_strips_trailing_zeros():
    assert quantity_str(Decimal("10")) == "10"
    assert quantity_str(Decimal("10.500000")) == "10.5"
    assert quantity_str(Decimal("0.125000")) == "0.125"
    assert quantity_str(Decimal("0")) == "0"


def test_quantity_str_never_emits_scientific_notation():
    assert quantity_str(Decimal("1E+1")) == "10"
    assert quantity_str(Decimal("1E-6")) == "0.000001"
    assert "E" not in quantity_str(Decimal("1E+3"))


def test_quantity_rejects_negative():
    with pytest.raises(ValueError):
        quantity("-1")


def test_decimal_value_refuses_float():
    with pytest.raises(ValueError):
        decimal_value(1.1)


def test_bps_money_rounds_once():
    assert bps_money(Decimal("1234.56"), Decimal("8")) == Decimal("0.99")
