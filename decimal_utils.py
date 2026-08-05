"""The only place monetary rounding and decimal formatting are defined.

No other module calls ``quantize`` with its own rules. The arena grades money
half away from zero at two decimals, and quantities as plain decimal strings
with no exponent notation.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

D = Decimal

ZERO = D("0")
CENT = D("0.01")
SHARE = D("0.000001")
BPS_DIVISOR = D("10000")


def decimal_value(value: str | int | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value

    if isinstance(value, float):
        raise ValueError("float is not an acceptable decimal source")

    try:
        return D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid decimal value") from exc


def money(value: str | int | Decimal) -> Decimal:
    return decimal_value(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value: str | int | Decimal) -> str:
    return format(money(value), ".2f")


def quantity(value: str | int | Decimal) -> Decimal:
    result = decimal_value(value)

    if result < ZERO:
        raise ValueError("quantity cannot be negative")

    return result


def share_quantity(value: str | int | Decimal) -> Decimal:
    """Round a derived share count to the six places quantities carry.

    Only a split produces a quantity the feed did not state, and a ratio like
    2:3 applied to a fractional reinvestment lot runs past six places. Rounding
    each lot keeps the position equal to the sum of its lots.
    """
    return decimal_value(value).quantize(SHARE, rounding=ROUND_HALF_UP)


def quantity_str(value: Decimal) -> str:
    text = format(decimal_value(value), "f")

    if "." in text:
        text = text.rstrip("0").rstrip(".")

    return text or "0"


def bps_raw(principal: Decimal, bps: Decimal) -> Decimal:
    return principal * bps / BPS_DIVISOR


def bps_money(principal: Decimal, bps: Decimal) -> Decimal:
    return money(bps_raw(principal, bps))
