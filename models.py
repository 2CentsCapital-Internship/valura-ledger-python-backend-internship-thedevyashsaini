"""Value types shared by the book, the engine, and the store.

Every monetary or quantity field is a ``Decimal``. Anything that crosses the
SQLite boundary or lands in a reversible effect is serialised through the
``to_dict``/``from_dict`` pairs here so that a replayed record rebuilds exactly
the object that was originally committed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from decimal_utils import ZERO, decimal_value, money_str, quantity_str

VALID_ACCOUNTS: set[str] = {
    "1100", "1150", "1200",
    "2010", "2100", "2300", "2350",
    "2400", "2411", "2412", "2413", "2420", "2430",
    "4000", "4010", "4100", "4200",
    "5000", "5010", "5100",
}


class RejectedEvent(Exception):
    """Raised by a handler for an event the book refuses to post.

    Carries a stable machine code so rejections can be grouped in diagnostics.
    Messages must never contain credentials or raw HTTP headers.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class JournalLeg:
    account: str
    customer_id: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.account not in VALID_ACCOUNTS:
            raise AssertionError(f"unknown account {self.account!r}")
        if not self.customer_id:
            raise AssertionError("leg is missing a customer id")
        if self.debit < ZERO or self.credit < ZERO:
            raise AssertionError("leg amounts must be nonnegative")
        if self.debit != ZERO and self.credit != ZERO:
            raise AssertionError("a leg carries either a debit or a credit")

    def to_payload(self) -> dict:
        return {
            "account": self.account,
            "customer_id": self.customer_id,
            "debit": money_str(self.debit),
            "credit": money_str(self.credit),
        }

    to_dict = to_payload

    @staticmethod
    def from_dict(value: dict) -> "JournalLeg":
        return JournalLeg(
            account=value["account"],
            customer_id=value["customer_id"],
            debit=decimal_value(value.get("debit", "0")),
            credit=decimal_value(value.get("credit", "0")),
        )


@dataclass(frozen=True)
class PreparedEvent:
    event_id: str
    event_type: str
    offset: int
    status: str
    legs: tuple[JournalLeg, ...]
    effect: dict
    rejection_reason: str | None = None


@dataclass(frozen=True)
class StoredEvent:
    run_id: str
    sequence_no: int
    offset: int
    event_id: str
    event_type: str
    raw_event: dict
    content_hash: str
    status: str
    rejection_reason: str | None
    legs: tuple[JournalLeg, ...]
    effect: dict


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    mode: str
    status: str
    started_at: str
    ended_at: str | None
    next_offset: int
    last_sequence_no: int


@dataclass
class Lot:
    lot_id: str
    source_event_id: str
    trade_id: str | None
    customer_id: str
    symbol: str
    quantity: Decimal
    total_cost: Decimal
    created_sequence: int

    def to_dict(self) -> dict:
        return {
            "lot_id": self.lot_id,
            "source_event_id": self.source_event_id,
            "trade_id": self.trade_id,
            "customer_id": self.customer_id,
            "symbol": self.symbol,
            "quantity": quantity_str(self.quantity),
            "total_cost": money_str(self.total_cost),
            "created_sequence": self.created_sequence,
        }

    @staticmethod
    def from_dict(value: dict) -> "Lot":
        return Lot(
            lot_id=value["lot_id"],
            source_event_id=value["source_event_id"],
            trade_id=value.get("trade_id"),
            customer_id=value["customer_id"],
            symbol=value["symbol"],
            quantity=decimal_value(value["quantity"]),
            total_cost=decimal_value(value["total_cost"]),
            created_sequence=int(value["created_sequence"]),
        )


@dataclass
class Order:
    order_id: str
    customer_id: str
    side: str
    symbol: str
    asset_class: str

    original_quantity: Decimal
    limit_price: Decimal
    estimated_principal: Decimal
    estimated_charges: Decimal

    route: str

    initial_cash_hold: Decimal
    remaining_cash_hold: Decimal

    initial_security_hold: Decimal
    remaining_security_hold: Decimal

    cumulative_fill_quantity: Decimal
    status: str

    placement_seen: bool
    lifecycle_events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "side": self.side,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "original_quantity": quantity_str(self.original_quantity),
            "limit_price": str(self.limit_price),
            "estimated_principal": money_str(self.estimated_principal),
            "estimated_charges": money_str(self.estimated_charges),
            "route": self.route,
            "initial_cash_hold": money_str(self.initial_cash_hold),
            "remaining_cash_hold": money_str(self.remaining_cash_hold),
            "initial_security_hold": quantity_str(self.initial_security_hold),
            "remaining_security_hold": quantity_str(self.remaining_security_hold),
            "cumulative_fill_quantity": quantity_str(self.cumulative_fill_quantity),
            "status": self.status,
            "placement_seen": self.placement_seen,
            "lifecycle_events": list(self.lifecycle_events),
        }

    @staticmethod
    def from_dict(value: dict) -> "Order":
        return Order(
            order_id=value["order_id"],
            customer_id=value["customer_id"],
            side=value["side"],
            symbol=value["symbol"],
            asset_class=value["asset_class"],
            original_quantity=decimal_value(value["original_quantity"]),
            limit_price=decimal_value(value["limit_price"]),
            estimated_principal=decimal_value(value["estimated_principal"]),
            estimated_charges=decimal_value(value["estimated_charges"]),
            route=value["route"],
            initial_cash_hold=decimal_value(value["initial_cash_hold"]),
            remaining_cash_hold=decimal_value(value["remaining_cash_hold"]),
            initial_security_hold=decimal_value(value["initial_security_hold"]),
            remaining_security_hold=decimal_value(value["remaining_security_hold"]),
            cumulative_fill_quantity=decimal_value(value["cumulative_fill_quantity"]),
            status=value["status"],
            placement_seen=bool(value["placement_seen"]),
            lifecycle_events=list(value.get("lifecycle_events", [])),
        )


@dataclass
class Trade:
    trade_id: str
    source_event_id: str
    order_id: str
    customer_id: str
    side: str
    principal: Decimal
    status: str

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "source_event_id": self.source_event_id,
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "side": self.side,
            "principal": money_str(self.principal),
            "status": self.status,
        }

    @staticmethod
    def from_dict(value: dict) -> "Trade":
        return Trade(
            trade_id=value["trade_id"],
            source_event_id=value["source_event_id"],
            order_id=value["order_id"],
            customer_id=value["customer_id"],
            side=value["side"],
            principal=decimal_value(value["principal"]),
            status=value["status"],
        )


@dataclass
class Withdrawal:
    withdrawal_id: str
    customer_id: str
    amount: Decimal
    status: str

    def to_dict(self) -> dict:
        return {
            "withdrawal_id": self.withdrawal_id,
            "customer_id": self.customer_id,
            "amount": money_str(self.amount),
            "status": self.status,
        }

    @staticmethod
    def from_dict(value: dict) -> "Withdrawal":
        return Withdrawal(
            withdrawal_id=value["withdrawal_id"],
            customer_id=value["customer_id"],
            amount=decimal_value(value["amount"]),
            status=value["status"],
        )


@dataclass
class FeeCharge:
    source_event_id: str
    customer_id: str
    amount: Decimal
    refunded: bool
    reversed: bool

    def to_dict(self) -> dict:
        return {
            "source_event_id": self.source_event_id,
            "customer_id": self.customer_id,
            "amount": money_str(self.amount),
            "refunded": self.refunded,
            "reversed": self.reversed,
        }

    @staticmethod
    def from_dict(value: dict) -> "FeeCharge":
        return FeeCharge(
            source_event_id=value["source_event_id"],
            customer_id=value["customer_id"],
            amount=decimal_value(value["amount"]),
            refunded=bool(value["refunded"]),
            reversed=bool(value["reversed"]),
        )


@dataclass(frozen=True)
class FillEconomics:
    brokerage: Decimal
    custody: Decimal
    regulatory: Decimal
    broker_cost: Decimal
    custody_cost: Decimal
    partner_share: Decimal
    broker_payable_account: str


@dataclass(frozen=True)
class FifoSlice:
    lot_id: str
    quantity: Decimal
    relieved_cost: Decimal


@dataclass(frozen=True)
class FifoPlan:
    slices: tuple[FifoSlice, ...]
    total_quantity: Decimal
    total_cost: Decimal
    before_lots: tuple[Lot, ...]
    after_lots: tuple[Lot, ...]
