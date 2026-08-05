"""The ledger itself: chart of accounts, tariffs, handlers, and the projection.

The book is a pure in-memory projection. ``prepare_event`` validates an event
and returns the legs plus a fully described, reversible state effect without
touching anything; ``apply_stored_event`` is the only method that mutates, and
it works from what was persisted rather than from a fresh calculation. That
split is what makes crash recovery and as-of replay reproduce exactly the book
that was originally committed.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, DivisionByZero, InvalidOperation

from decimal_utils import (
    D,
    ZERO,
    bps_raw,
    decimal_value,
    money,
    money_str,
    quantity_str,
)
from models import (
    FeeCharge,
    FifoPlan,
    FifoSlice,
    FillEconomics,
    JournalLeg,
    Lot,
    Order,
    PreparedEvent,
    RejectedEvent,
    StoredEvent,
    Trade,
    Withdrawal,
)

ACCOUNT_OMNIBUS_CASH = "1100"
ACCOUNT_SETTLEMENT_RECEIVABLE = "1150"
ACCOUNT_OMNIBUS_CUSTODY = "1200"

ACCOUNT_CUSTOMER_WALLET = "2010"
ACCOUNT_CUSTOMER_SECURITIES_CLAIM = "2100"
ACCOUNT_WITHDRAWALS_IN_TRANSIT = "2300"
ACCOUNT_UNSETTLED_TRADE_PAYABLE = "2350"

ACCOUNT_REG_FEES_PAYABLE = "2400"
ACCOUNT_BROKER_FEES_BRK_A = "2411"
ACCOUNT_BROKER_FEES_BRK_B = "2412"
ACCOUNT_BROKER_FEES_BRK_C = "2413"
ACCOUNT_CUSTODIAN_FEES_PAYABLE = "2420"
ACCOUNT_PARTNER_SHARE_PAYABLE = "2430"

ACCOUNT_BROKERAGE_REVENUE = "4000"
ACCOUNT_CUSTODY_REVENUE = "4010"
ACCOUNT_FX_SPREAD_REVENUE = "4100"
ACCOUNT_INTEREST_INCOME = "4200"

ACCOUNT_BROKERAGE_COST = "5000"
ACCOUNT_CUSTODY_COST = "5010"
ACCOUNT_PARTNER_REVENUE_SHARE = "5100"

ALL_ACCOUNTS = {
    "1100", "1150", "1200",
    "2010", "2100", "2300", "2350",
    "2400", "2411", "2412", "2413", "2420", "2430",
    "4000", "4010", "4100", "4200",
    "5000", "5010", "5100",
}

REGULATORY_BPS = D("8")

TARIFFS: dict[str, dict] = {
    "BRK-A": {
        "asset_classes": {"equity", "etf"},
        "brokerage_bps": D("20"),
        "custody_bps": D("4"),
        "broker_cost_bps": D("9"),
        "custody_cost_bps": D("2"),
        "minimum_fee": D("1.00"),
        "ticket_fee": D("0.35"),
        "payable_account": ACCOUNT_BROKER_FEES_BRK_A,
    },
    "BRK-B": {
        "asset_classes": {"equity", "bond"},
        "brokerage_bps": D("15"),
        "custody_bps": D("5"),
        "broker_cost_bps": D("8"),
        "custody_cost_bps": D("3"),
        "minimum_fee": D("2.50"),
        "ticket_fee": D("3.00"),
        "payable_account": ACCOUNT_BROKER_FEES_BRK_B,
    },
    "BRK-C": {
        "asset_classes": {"etf", "bond"},
        "brokerage_bps": D("25"),
        "custody_bps": D("3"),
        "broker_cost_bps": D("12"),
        "custody_cost_bps": D("1"),
        "minimum_fee": D("0.50"),
        "ticket_fee": D("0.20"),
        "payable_account": ACCOUNT_BROKER_FEES_BRK_C,
    },
}

ASSET_CLASSES = {"equity", "etf", "bond"}
SIDES = {"buy", "sell"}

NO_EFFECT: dict = {"operations": []}


def validate_balanced(legs: tuple[JournalLeg, ...]) -> None:
    total_debit = money(sum((leg.debit for leg in legs), ZERO))
    total_credit = money(sum((leg.credit for leg in legs), ZERO))

    if total_debit != total_credit:
        raise AssertionError(
            f"unbalanced journal: debit={total_debit}, credit={total_credit}"
        )


def choose_broker(
    asset_class: str,
    quantity: Decimal,
    limit_price: Decimal,
) -> str:
    estimated_principal = money(quantity * limit_price)

    candidates = []
    for broker_id, tariff in TARIFFS.items():
        if asset_class not in tariff["asset_classes"]:
            continue

        brokerage = max(
            money(bps_raw(estimated_principal, tariff["brokerage_bps"])),
            tariff["minimum_fee"],
        )
        custody = money(bps_raw(estimated_principal, tariff["custody_bps"]))

        candidates.append((brokerage + custody, broker_id))

    if not candidates:
        raise RejectedEvent(
            "unsupported_asset_class",
            f"no broker trades asset class {asset_class!r}",
        )

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def compute_fill_economics(
    principal: Decimal,
    broker: str,
    partner_rate: Decimal,
) -> FillEconomics:
    tariff = TARIFFS[broker]

    raw_brokerage = bps_raw(principal, tariff["brokerage_bps"])
    brokerage = max(money(raw_brokerage), tariff["minimum_fee"])

    custody = money(bps_raw(principal, tariff["custody_bps"]))
    regulatory = money(bps_raw(principal, REGULATORY_BPS))

    broker_cost = money(
        bps_raw(principal, tariff["broker_cost_bps"]) + tariff["ticket_fee"]
    )
    custody_cost = money(bps_raw(principal, tariff["custody_cost_bps"]))

    margin = brokerage + custody - broker_cost - custody_cost
    positive_margin = max(margin, ZERO)
    partner_share = money(partner_rate * positive_margin)

    return FillEconomics(
        brokerage=brokerage,
        custody=custody,
        regulatory=regulatory,
        broker_cost=broker_cost,
        custody_cost=custody_cost,
        partner_share=partner_share,
        broker_payable_account=tariff["payable_account"],
    )


class Book:
    def __init__(self) -> None:
        self.balances: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)

        self.accounts_ever_used: set[str] = set()
        self.customers_seen: set[str] = set()

        self.fee_charges: dict[str, FeeCharge] = {}
        self.withdrawals: dict[str, Withdrawal] = {}

        self.orders: dict[str, Order] = {}
        self.pending_order_lifecycle: dict[str, list[dict]] = defaultdict(list)

        self.trades: dict[str, Trade] = {}

        self.lots: dict[tuple[str, str], list[Lot]] = defaultdict(list)

        self.event_records: dict[str, dict] = {}
        self.reversed_event_ids: set[str] = set()

        # Drained by the engine after every prepare_event call. Diagnostics
        # only: never accounting state, never replayed.
        self.warnings: list[dict] = []
        self._reversal_guard: set[str] = set()

    # -- mutation ----------------------------------------------------------
    def apply_legs(self, legs: tuple[JournalLeg, ...]) -> None:
        for item in legs:
            key = (item.customer_id, item.account)

            self.balances[key] += item.debit - item.credit
            self.accounts_ever_used.add(item.account)
            self.customers_seen.add(item.customer_id)

    def apply_effect(self, effect: dict, reverse: bool = False) -> None:
        operations = effect.get("operations", [])

        if reverse:
            operations = list(reversed(operations))

        for operation in operations:
            if reverse and not operation.get("reversible", True):
                continue

            value = operation["before"] if reverse else operation["after"]

            self._apply_effect_operation(
                operation["type"],
                value,
                operation.get("key"),
            )

    def _apply_effect_operation(self, op_type: str, value, key) -> None:
        if op_type == "set_fee_charge":
            if value is None:
                self.fee_charges.pop(key, None)
            else:
                self.fee_charges[key] = FeeCharge.from_dict(value)

        elif op_type == "set_withdrawal":
            if value is None:
                self.withdrawals.pop(key, None)
            else:
                self.withdrawals[key] = Withdrawal.from_dict(value)

        elif op_type == "set_order":
            if value is None:
                self.orders.pop(key, None)
            else:
                self.orders[key] = Order.from_dict(value)

        elif op_type == "set_pending_order_lifecycle":
            if not value:
                self.pending_order_lifecycle.pop(key, None)
            else:
                self.pending_order_lifecycle[key] = [dict(x) for x in value]

        elif op_type == "set_trade":
            if value is None:
                self.trades.pop(key, None)
            else:
                self.trades[key] = Trade.from_dict(value)

        elif op_type == "add_lot":
            bucket = (key["customer_id"], key["symbol"])
            if value is None:
                self.lots[bucket] = [
                    lot for lot in self.lots.get(bucket, [])
                    if lot.lot_id != key["lot_id"]
                ]
            else:
                lot = Lot.from_dict(value)
                existing = self.lots[bucket]
                if not any(item.lot_id == lot.lot_id for item in existing):
                    existing.append(lot)

        elif op_type == "update_lot":
            bucket = (key["customer_id"], key["symbol"])
            lot = Lot.from_dict(value)
            existing = self.lots.get(bucket, [])
            for index, item in enumerate(existing):
                if item.lot_id == lot.lot_id:
                    existing[index] = lot
                    break

        elif op_type == "remove_lot":
            bucket = (key["customer_id"], key["symbol"])
            existing = self.lots[bucket]
            if value is None:
                self.lots[bucket] = [
                    item for item in existing if item.lot_id != key["lot_id"]
                ]
            else:
                lot = Lot.from_dict(value)
                if not any(item.lot_id == lot.lot_id for item in existing):
                    existing.insert(int(key.get("index", len(existing))), lot)

        elif op_type == "set_lot_collection":
            bucket = (key["customer_id"], key["symbol"])
            self.lots[bucket] = [Lot.from_dict(item) for item in (value or [])]

        elif op_type == "rename_lots":
            customer_id = key["customer_id"]
            for symbol, lots in (value or {}).items():
                self.lots[(customer_id, symbol)] = [
                    Lot.from_dict(item) for item in lots
                ]

        elif op_type == "mark_event_reversed":
            if value and value.get("reversed"):
                self.reversed_event_ids.add(value["event_id"])
            elif value is not None:
                self.reversed_event_ids.discard(value["event_id"])

        elif op_type == "reverse_event_effect":
            self._apply_reverse_event_effect(value)

        else:
            raise AssertionError(f"unknown effect operation {op_type!r}")

    def _apply_reverse_event_effect(self, value) -> None:
        if not value:
            return

        target_id = value["target_event_id"]
        want_reversed = bool(value["target_was_reversed"])

        if target_id in self._reversal_guard:
            return

        record = self.event_records.get(target_id)
        if record is None:
            return

        self._reversal_guard.add(target_id)
        try:
            if want_reversed and target_id not in self.reversed_event_ids:
                self.apply_effect(record["effect"], reverse=True)
                self.reversed_event_ids.add(target_id)
            elif not want_reversed and target_id in self.reversed_event_ids:
                self.apply_effect(record["effect"], reverse=False)
                self.reversed_event_ids.discard(target_id)
        finally:
            self._reversal_guard.discard(target_id)

    def apply_stored_event(self, event: StoredEvent) -> None:
        self.event_records[event.event_id] = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "status": event.status,
            "legs": event.legs,
            "effect": event.effect,
            "sequence_no": event.sequence_no,
        }

        if event.status != "accepted":
            return

        self.apply_legs(event.legs)
        self.apply_effect(event.effect, reverse=False)

    # -- preparation -------------------------------------------------------
    def prepare_event(self, event: dict, sequence_no: int) -> PreparedEvent:
        self.warnings = []

        event_id = event["event_id"]
        event_type = event["type"]
        offset = int(event["offset"])
        payload = event.get("payload")

        def rejected(code: str) -> PreparedEvent:
            return PreparedEvent(
                event_id=event_id,
                event_type=event_type,
                offset=offset,
                status="rejected",
                legs=(),
                effect=dict(NO_EFFECT),
                rejection_reason=code,
            )

        if not isinstance(payload, dict):
            return rejected("malformed_payload")

        handler = getattr(self, "on_" + event_type, None)
        if handler is None:
            return rejected("unknown_event_type")

        try:
            legs_list, effect = handler(payload, event, sequence_no)
        except RejectedEvent as exc:
            return rejected(exc.code)
        except (InvalidOperation, DivisionByZero, ValueError, TypeError, KeyError):
            return rejected("malformed_payload")
        except AssertionError:
            return rejected("internal_invariant")

        legs = tuple(leg for leg in legs_list if leg.debit != ZERO or leg.credit != ZERO)
        validate_balanced(legs)

        return PreparedEvent(
            event_id=event_id,
            event_type=event_type,
            offset=offset,
            status="accepted",
            legs=legs,
            effect=effect or dict(NO_EFFECT),
            rejection_reason=None,
        )

    # -- payload helpers ---------------------------------------------------
    def _warn(self, code: str, **details) -> None:
        entry = {"code": code}
        entry.update(details)
        self.warnings.append(entry)

    @staticmethod
    def _text(payload: dict, name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise RejectedEvent("malformed_payload", f"missing field {name}")
        return value

    @staticmethod
    def _amount(payload: dict, name: str) -> Decimal:
        if name not in payload:
            raise RejectedEvent("malformed_payload", f"missing field {name}")
        try:
            return money(decimal_value(payload[name]))
        except (ValueError, InvalidOperation) as exc:
            raise RejectedEvent(
                "malformed_payload", f"field {name} is not a decimal"
            ) from exc

    @classmethod
    def _positive_amount(cls, payload: dict, name: str) -> Decimal:
        value = cls._amount(payload, name)
        if value <= ZERO:
            raise RejectedEvent("nonpositive_amount", f"field {name} must be positive")
        return value

    @staticmethod
    def _decimal(payload: dict, name: str) -> Decimal:
        if name not in payload:
            raise RejectedEvent("malformed_payload", f"missing field {name}")
        try:
            return decimal_value(payload[name])
        except (ValueError, InvalidOperation) as exc:
            raise RejectedEvent(
                "malformed_payload", f"field {name} is not a decimal"
            ) from exc

    @classmethod
    def _positive_decimal(cls, payload: dict, name: str) -> Decimal:
        value = cls._decimal(payload, name)
        if value <= ZERO:
            raise RejectedEvent("nonpositive_amount", f"field {name} must be positive")
        return value

    # -- cash --------------------------------------------------------------
    def on_deposit(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        amount = self._positive_amount(p, "amount")

        legs = [
            JournalLeg(ACCOUNT_OMNIBUS_CASH, customer_id, debit=amount),
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, credit=amount),
        ]
        return legs, dict(NO_EFFECT)

    def on_fee_charged(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        amount = self._positive_amount(p, "amount")

        event_id = ev["event_id"]
        if event_id in self.fee_charges:
            raise RejectedEvent("duplicate_fee_charge", "fee already recorded")

        legs = [
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, debit=amount),
            JournalLeg(ACCOUNT_OMNIBUS_CASH, customer_id, credit=amount),
        ]

        charge = FeeCharge(
            source_event_id=event_id,
            customer_id=customer_id,
            amount=amount,
            refunded=False,
            reversed=False,
        )
        effect = {
            "operations": [
                {
                    "type": "set_fee_charge",
                    "key": event_id,
                    "reversible": True,
                    "before": None,
                    "after": charge.to_dict(),
                }
            ]
        }
        return legs, effect

    def on_fee_refund(self, p: dict, ev: dict, seq: int):
        source_id = self._text(p, "refunds_source_id")
        customer_id = self._text(p, "customer_id")

        charge = self.fee_charges.get(source_id)
        if charge is None:
            raise RejectedEvent("unknown_fee_source", "fee_charged source not found")
        if charge.customer_id != customer_id:
            raise RejectedEvent("fee_customer_mismatch", "refund names another customer")
        if charge.reversed:
            raise RejectedEvent("fee_reversed", "source fee was reversed")
        if charge.refunded:
            raise RejectedEvent("fee_already_refunded", "fee already refunded")

        amount = charge.amount
        legs = [
            JournalLeg(ACCOUNT_OMNIBUS_CASH, customer_id, debit=amount),
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, credit=amount),
        ]

        after = FeeCharge(
            source_event_id=charge.source_event_id,
            customer_id=charge.customer_id,
            amount=charge.amount,
            refunded=True,
            reversed=charge.reversed,
        )
        effect = {
            "operations": [
                {
                    "type": "set_fee_charge",
                    "key": source_id,
                    "reversible": True,
                    "before": charge.to_dict(),
                    "after": after.to_dict(),
                }
            ]
        }
        return legs, effect

    def on_withdrawal_requested(self, p: dict, ev: dict, seq: int):
        withdrawal_id = self._text(p, "withdrawal_id")
        customer_id = self._text(p, "customer_id")
        amount = self._positive_amount(p, "amount")

        if withdrawal_id in self.withdrawals:
            raise RejectedEvent("duplicate_withdrawal", "withdrawal id already used")

        legs = [
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, debit=amount),
            JournalLeg(ACCOUNT_WITHDRAWALS_IN_TRANSIT, customer_id, credit=amount),
        ]

        withdrawal = Withdrawal(
            withdrawal_id=withdrawal_id,
            customer_id=customer_id,
            amount=amount,
            status="requested",
        )
        effect = {
            "operations": [
                {
                    "type": "set_withdrawal",
                    "key": withdrawal_id,
                    "reversible": True,
                    "before": None,
                    "after": withdrawal.to_dict(),
                }
            ]
        }
        return legs, effect

    def _withdrawal_close(self, p: dict, status: str, credit_account: str):
        withdrawal_id = self._text(p, "withdrawal_id")

        withdrawal = self.withdrawals.get(withdrawal_id)
        if withdrawal is None:
            raise RejectedEvent("unknown_withdrawal", "withdrawal id not found")
        if withdrawal.status != "requested":
            raise RejectedEvent(
                "withdrawal_not_open",
                f"withdrawal is {withdrawal.status}",
            )

        amount = withdrawal.amount
        legs = [
            JournalLeg(
                ACCOUNT_WITHDRAWALS_IN_TRANSIT,
                withdrawal.customer_id,
                debit=amount,
            ),
            JournalLeg(credit_account, withdrawal.customer_id, credit=amount),
        ]

        after = Withdrawal(
            withdrawal_id=withdrawal.withdrawal_id,
            customer_id=withdrawal.customer_id,
            amount=withdrawal.amount,
            status=status,
        )
        effect = {
            "operations": [
                {
                    "type": "set_withdrawal",
                    "key": withdrawal_id,
                    "reversible": True,
                    "before": withdrawal.to_dict(),
                    "after": after.to_dict(),
                }
            ]
        }
        return legs, effect

    def on_withdrawal_settled(self, p: dict, ev: dict, seq: int):
        return self._withdrawal_close(p, "settled", ACCOUNT_OMNIBUS_CASH)

    def on_withdrawal_rejected(self, p: dict, ev: dict, seq: int):
        return self._withdrawal_close(p, "rejected", ACCOUNT_CUSTOMER_WALLET)

    def on_interest_credited(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        gross = self._amount(p, "gross_amount")
        customer_share = self._amount(p, "customer_share")

        if gross < ZERO:
            raise RejectedEvent("negative_amount", "gross_amount is negative")
        if customer_share < ZERO:
            raise RejectedEvent("negative_amount", "customer_share is negative")
        if customer_share > gross:
            raise RejectedEvent("share_exceeds_gross", "customer share exceeds gross")

        firm_share = money(gross - customer_share)
        if firm_share < ZERO:
            raise RejectedEvent("negative_amount", "firm share is negative")

        legs = [
            JournalLeg(ACCOUNT_OMNIBUS_CASH, customer_id, debit=gross),
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, credit=customer_share),
            JournalLeg(ACCOUNT_INTEREST_INCOME, customer_id, credit=firm_share),
        ]
        return legs, dict(NO_EFFECT)

    def on_transfer_between_customers(self, p: dict, ev: dict, seq: int):
        sender = self._text(p, "from_customer_id")
        recipient = self._text(p, "to_customer_id")
        amount = self._positive_amount(p, "amount")

        if sender == recipient:
            raise RejectedEvent("self_transfer", "sender equals recipient")

        legs = [
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, sender, debit=amount),
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, recipient, credit=amount),
        ]
        return legs, dict(NO_EFFECT)

    def on_fx_deposit(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        amount_foreign = self._positive_decimal(p, "amount_foreign")
        market_rate = self._positive_decimal(p, "market_rate")
        customer_rate = self._positive_decimal(p, "customer_rate")

        market_usd = self._positive_amount(p, "usd_at_market_rate")
        customer_usd = self._positive_amount(p, "usd_at_customer_rate")

        if customer_usd > market_usd:
            raise RejectedEvent("negative_fx_spread", "customer rate beats market rate")

        spread = money(market_usd - customer_usd)
        if spread < ZERO:
            raise RejectedEvent("negative_fx_spread", "fx spread is negative")

        # The feed quotes rates as foreign units per USD, so the conversion is
        # a division. Accept either direction rather than assume: the warning
        # is for amounts no reading of the rate reproduces.
        consistent = any(
            money(convert(amount_foreign, market_rate)) == market_usd
            and money(convert(amount_foreign, customer_rate)) == customer_usd
            for convert in (lambda a, r: a / r, lambda a, r: a * r)
        )
        if not consistent:
            self._warn(
                "fx_conversion_mismatch",
                event_type=ev["type"],
                amount_foreign=str(amount_foreign),
                market_rate=str(market_rate),
                customer_rate=str(customer_rate),
                actual_market=money_str(market_usd),
                actual_customer=money_str(customer_usd),
            )

        legs = [
            JournalLeg(ACCOUNT_OMNIBUS_CASH, customer_id, debit=market_usd),
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, credit=customer_usd),
            JournalLeg(ACCOUNT_FX_SPREAD_REVENUE, customer_id, credit=spread),
        ]
        return legs, dict(NO_EFFECT)

    # -- orders ------------------------------------------------------------
    def position_quantity(self, customer_id: str, symbol: str) -> Decimal:
        return sum(
            (lot.quantity for lot in self.lots.get((customer_id, symbol), [])),
            ZERO,
        )

    def _security_held_by_other_orders(
        self,
        customer_id: str,
        symbol: str,
        order_id: str,
    ) -> Decimal:
        total = ZERO
        for order in self.orders.values():
            if order.order_id == order_id:
                continue
            if order.customer_id != customer_id or order.symbol != symbol:
                continue
            if order.side != "sell" or order.status != "open":
                continue
            total += order.remaining_security_hold
        return total

    @staticmethod
    def _released(order: Order, fill_quantity: Decimal, final: bool) -> Order:
        result = Order.from_dict(order.to_dict())
        result.cumulative_fill_quantity += fill_quantity

        if final:
            result.remaining_cash_hold = ZERO
            result.remaining_security_hold = ZERO
            result.status = "filled"
            return result

        if result.original_quantity > ZERO:
            cash_release = money(
                result.initial_cash_hold * fill_quantity / result.original_quantity
            )
            security_release = (
                result.initial_security_hold * fill_quantity / result.original_quantity
            )
        else:
            cash_release = result.remaining_cash_hold
            security_release = result.remaining_security_hold

        cash_release = min(cash_release, result.remaining_cash_hold)
        security_release = min(security_release, result.remaining_security_hold)

        result.remaining_cash_hold = money(result.remaining_cash_hold - cash_release)
        result.remaining_security_hold = (
            result.remaining_security_hold - security_release
        )
        return result

    def on_order_placed(self, p: dict, ev: dict, seq: int):
        order_id = self._text(p, "order_id")
        customer_id = self._text(p, "customer_id")
        side = self._text(p, "side")
        symbol = self._text(p, "symbol")
        asset_class = self._text(p, "asset_class")
        quantity = self._positive_decimal(p, "quantity")
        limit_price = self._positive_decimal(p, "limit_price")
        est_charges = self._amount(p, "est_charges")

        if side not in SIDES:
            raise RejectedEvent("invalid_side", f"side {side!r} is not buy or sell")
        if asset_class not in ASSET_CLASSES:
            raise RejectedEvent(
                "invalid_asset_class", f"asset class {asset_class!r} is unknown"
            )
        if est_charges < ZERO:
            raise RejectedEvent("negative_amount", "est_charges is negative")

        existing = self.orders.get(order_id)
        if existing is not None and existing.placement_seen:
            raise RejectedEvent("duplicate_order", "order already placed")

        route = choose_broker(asset_class, quantity, limit_price)
        estimated_principal = money(quantity * limit_price)

        if side == "buy":
            initial_cash_hold = money(estimated_principal + est_charges)
            initial_security_hold = ZERO
        else:
            initial_cash_hold = ZERO
            initial_security_hold = quantity

        order = Order(
            order_id=order_id,
            customer_id=customer_id,
            side=side,
            symbol=symbol,
            asset_class=asset_class,
            original_quantity=quantity,
            limit_price=limit_price,
            estimated_principal=estimated_principal,
            estimated_charges=est_charges,
            route=route,
            initial_cash_hold=initial_cash_hold,
            remaining_cash_hold=initial_cash_hold,
            initial_security_hold=initial_security_hold,
            remaining_security_hold=initial_security_hold,
            cumulative_fill_quantity=ZERO,
            status="open",
            placement_seen=True,
            lifecycle_events=[],
        )

        pending = sorted(
            self.pending_order_lifecycle.get(order_id, []),
            key=lambda entry: entry["sequence"],
        )

        for entry in pending:
            kind = entry["kind"]
            if kind in {"partial_fill", "final_fill"}:
                fill_quantity = decimal_value(entry["quantity"])
                order = self._released(order, fill_quantity, kind == "final_fill")
            elif kind in {"cancelled", "rejected"}:
                order.remaining_cash_hold = ZERO
                order.remaining_security_hold = ZERO
                order.status = kind
            order.lifecycle_events.append(dict(entry))

        if order.cumulative_fill_quantity > quantity:
            raise RejectedEvent(
                "fills_exceed_order",
                "observed fills exceed the placed quantity",
            )

        if side == "sell" and order.remaining_security_hold > ZERO:
            available = self.position_quantity(
                customer_id, symbol
            ) - self._security_held_by_other_orders(customer_id, symbol, order_id)
            if order.remaining_security_hold > available:
                raise RejectedEvent(
                    "insufficient_position",
                    "sell hold exceeds the sellable position",
                )

        operations = [
            {
                "type": "set_order",
                "key": order_id,
                "reversible": True,
                "before": existing.to_dict() if existing is not None else None,
                "after": order.to_dict(),
            }
        ]
        if pending:
            operations.append(
                {
                    "type": "set_pending_order_lifecycle",
                    "key": order_id,
                    "reversible": True,
                    "before": [dict(entry) for entry in pending],
                    "after": None,
                }
            )

        return [], {"operations": operations}

    def _order_close(self, p: dict, ev: dict, seq: int, status: str):
        order_id = self._text(p, "order_id")

        order = self.orders.get(order_id)
        if order is not None and order.placement_seen:
            if order.status != "open":
                raise RejectedEvent(
                    "order_not_open", f"order is already {order.status}"
                )

            after = Order.from_dict(order.to_dict())
            after.remaining_cash_hold = ZERO
            after.remaining_security_hold = ZERO
            after.status = status

            effect = {
                "operations": [
                    {
                        "type": "set_order",
                        "key": order_id,
                        "reversible": True,
                        "before": order.to_dict(),
                        "after": after.to_dict(),
                    }
                ]
            }
            return [], effect

        pending = [dict(x) for x in self.pending_order_lifecycle.get(order_id, [])]
        if any(entry["kind"] in {"cancelled", "rejected", "final_fill"} for entry in pending):
            raise RejectedEvent("order_not_open", "order lifecycle already closed")

        after = pending + [{"sequence": seq, "kind": status}]
        effect = {
            "operations": [
                {
                    "type": "set_pending_order_lifecycle",
                    "key": order_id,
                    "reversible": True,
                    "before": pending or None,
                    "after": after,
                }
            ]
        }
        return [], effect

    def on_order_cancelled(self, p: dict, ev: dict, seq: int):
        return self._order_close(p, ev, seq, "cancelled")

    def on_order_rejected(self, p: dict, ev: dict, seq: int):
        return self._order_close(p, ev, seq, "rejected")

    # -- fills -------------------------------------------------------------
    def plan_fifo_sale(
        self,
        customer_id: str,
        symbol: str,
        quantity_to_sell: Decimal,
    ) -> FifoPlan:
        lots = self.lots.get((customer_id, symbol), [])
        available = sum((lot.quantity for lot in lots), ZERO)

        if quantity_to_sell > available:
            raise RejectedEvent(
                "oversell",
                "sale quantity exceeds the held position",
            )

        remaining = quantity_to_sell
        slices: list[FifoSlice] = []
        after_lots: list[Lot] = []
        total_cost = ZERO

        for lot in lots:
            if remaining <= ZERO:
                after_lots.append(
                    Lot(
                        lot_id=lot.lot_id,
                        source_event_id=lot.source_event_id,
                        trade_id=lot.trade_id,
                        customer_id=lot.customer_id,
                        symbol=lot.symbol,
                        quantity=lot.quantity,
                        total_cost=lot.total_cost,
                        created_sequence=lot.created_sequence,
                    )
                )
                continue

            consumed = min(lot.quantity, remaining)

            if consumed >= lot.quantity:
                relieved = lot.total_cost
            else:
                relieved = money(lot.total_cost * consumed / lot.quantity)

            slices.append(
                FifoSlice(lot_id=lot.lot_id, quantity=consumed, relieved_cost=relieved)
            )
            total_cost += relieved
            remaining -= consumed

            residual_quantity = lot.quantity - consumed
            if residual_quantity > ZERO:
                after_lots.append(
                    Lot(
                        lot_id=lot.lot_id,
                        source_event_id=lot.source_event_id,
                        trade_id=lot.trade_id,
                        customer_id=lot.customer_id,
                        symbol=lot.symbol,
                        quantity=residual_quantity,
                        total_cost=money(lot.total_cost - relieved),
                        created_sequence=lot.created_sequence,
                    )
                )

        return FifoPlan(
            slices=tuple(slices),
            total_quantity=quantity_to_sell,
            total_cost=money(total_cost),
            before_lots=tuple(lots),
            after_lots=tuple(after_lots),
        )

    def _lifecycle_operation(
        self,
        order_id: str,
        fill_quantity: Decimal,
        final: bool,
        seq: int,
    ) -> dict:
        order = self.orders.get(order_id)

        if order is not None and order.placement_seen:
            after = self._released(order, fill_quantity, final)
            return {
                "type": "set_order",
                "key": order_id,
                "reversible": False,
                "before": order.to_dict(),
                "after": after.to_dict(),
            }

        pending = [dict(x) for x in self.pending_order_lifecycle.get(order_id, [])]
        entry = {
            "sequence": seq,
            "kind": "final_fill" if final else "partial_fill",
            "quantity": quantity_str(fill_quantity),
        }
        return {
            "type": "set_pending_order_lifecycle",
            "key": order_id,
            "reversible": False,
            "before": pending or None,
            "after": pending + [entry],
        }

    def _fill(self, p: dict, ev: dict, seq: int, final: bool):
        order_id = self._text(p, "order_id")
        customer_id = self._text(p, "customer_id")
        side = self._text(p, "side")
        symbol = self._text(p, "symbol")
        asset_class = self._text(p, "asset_class")
        broker = self._text(p, "broker")
        trade_id = self._text(p, "trade_id")

        fill_quantity = self._positive_decimal(p, "quantity")
        price = self._positive_decimal(p, "price")
        principal = self._positive_amount(p, "principal")
        partner_rate = self._decimal(p, "partner_rate")

        if side not in SIDES:
            raise RejectedEvent("invalid_side", f"side {side!r} is not buy or sell")
        if partner_rate < ZERO:
            raise RejectedEvent("negative_amount", "partner_rate is negative")
        if broker not in TARIFFS:
            raise RejectedEvent("unknown_broker", f"broker {broker!r} is unknown")
        if asset_class not in TARIFFS[broker]["asset_classes"]:
            raise RejectedEvent(
                "broker_asset_class_mismatch",
                f"{broker} does not trade {asset_class}",
            )
        if trade_id in self.trades:
            raise RejectedEvent("duplicate_trade", "trade id already recorded")

        expected_principal = money(fill_quantity * price)
        if expected_principal != principal:
            self._warn(
                "principal_mismatch",
                event_type=ev["type"],
                expected=money_str(expected_principal),
                actual=money_str(principal),
            )

        order = self.orders.get(order_id)
        if order is not None and order.placement_seen and order.route != broker:
            self._warn(
                "route_mismatch",
                event_type=ev["type"],
                expected=order.route,
                actual=broker,
            )

        economics = compute_fill_economics(principal, broker, partner_rate)

        trade = Trade(
            trade_id=trade_id,
            source_event_id=ev["event_id"],
            order_id=order_id,
            customer_id=customer_id,
            side=side,
            principal=principal,
            status="unsettled",
        )
        trade_operation = {
            "type": "set_trade",
            "key": trade_id,
            "reversible": True,
            "before": None,
            "after": trade.to_dict(),
        }
        lifecycle_operation = self._lifecycle_operation(
            order_id, fill_quantity, final, seq
        )

        if side == "buy":
            legs, operations = self._buy_legs(
                ev, seq, customer_id, symbol, trade_id, fill_quantity, principal, economics
            )
        else:
            legs, operations = self._sell_legs(
                customer_id, symbol, fill_quantity, principal, economics
            )

        operations.append(trade_operation)
        operations.append(lifecycle_operation)
        return legs, {"operations": operations}

    def _buy_legs(
        self,
        ev: dict,
        seq: int,
        customer_id: str,
        symbol: str,
        trade_id: str,
        fill_quantity: Decimal,
        principal: Decimal,
        economics: FillEconomics,
    ):
        wallet_debit = money(
            principal + economics.brokerage + economics.custody + economics.regulatory
        )

        legs = [
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, debit=wallet_debit),
            JournalLeg(ACCOUNT_OMNIBUS_CUSTODY, customer_id, debit=principal),
            JournalLeg(ACCOUNT_BROKERAGE_COST, customer_id, debit=economics.broker_cost),
            JournalLeg(ACCOUNT_CUSTODY_COST, customer_id, debit=economics.custody_cost),
            JournalLeg(
                ACCOUNT_PARTNER_REVENUE_SHARE, customer_id, debit=economics.partner_share
            ),
            JournalLeg(ACCOUNT_UNSETTLED_TRADE_PAYABLE, customer_id, credit=principal),
            JournalLeg(
                ACCOUNT_CUSTOMER_SECURITIES_CLAIM, customer_id, credit=principal
            ),
            JournalLeg(
                ACCOUNT_BROKERAGE_REVENUE, customer_id, credit=economics.brokerage
            ),
            JournalLeg(ACCOUNT_CUSTODY_REVENUE, customer_id, credit=economics.custody),
            JournalLeg(
                ACCOUNT_REG_FEES_PAYABLE, customer_id, credit=economics.regulatory
            ),
            JournalLeg(
                economics.broker_payable_account,
                customer_id,
                credit=economics.broker_cost,
            ),
            JournalLeg(
                ACCOUNT_CUSTODIAN_FEES_PAYABLE,
                customer_id,
                credit=economics.custody_cost,
            ),
            JournalLeg(
                ACCOUNT_PARTNER_SHARE_PAYABLE,
                customer_id,
                credit=economics.partner_share,
            ),
        ]

        lot = Lot(
            lot_id=f"lot:{ev['event_id']}",
            source_event_id=ev["event_id"],
            trade_id=trade_id,
            customer_id=customer_id,
            symbol=symbol,
            quantity=fill_quantity,
            total_cost=principal,
            created_sequence=seq,
        )
        operations = [
            {
                "type": "add_lot",
                "key": {
                    "customer_id": customer_id,
                    "symbol": symbol,
                    "lot_id": lot.lot_id,
                },
                "reversible": True,
                "before": None,
                "after": lot.to_dict(),
            }
        ]
        return legs, operations

    def _sell_legs(
        self,
        customer_id: str,
        symbol: str,
        fill_quantity: Decimal,
        principal: Decimal,
        economics: FillEconomics,
    ):
        plan = self.plan_fifo_sale(customer_id, symbol, fill_quantity)
        relieved = plan.total_cost

        wallet_credit = money(
            principal - economics.brokerage - economics.custody - economics.regulatory
        )
        if wallet_credit < ZERO:
            raise RejectedEvent(
                "charges_exceed_principal",
                "sale charges exceed the sale proceeds",
            )

        legs = [
            JournalLeg(ACCOUNT_SETTLEMENT_RECEIVABLE, customer_id, debit=principal),
            JournalLeg(
                ACCOUNT_CUSTOMER_SECURITIES_CLAIM, customer_id, debit=relieved
            ),
            JournalLeg(ACCOUNT_BROKERAGE_COST, customer_id, debit=economics.broker_cost),
            JournalLeg(ACCOUNT_CUSTODY_COST, customer_id, debit=economics.custody_cost),
            JournalLeg(
                ACCOUNT_PARTNER_REVENUE_SHARE, customer_id, debit=economics.partner_share
            ),
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, credit=wallet_credit),
            JournalLeg(ACCOUNT_OMNIBUS_CUSTODY, customer_id, credit=relieved),
            JournalLeg(
                ACCOUNT_BROKERAGE_REVENUE, customer_id, credit=economics.brokerage
            ),
            JournalLeg(ACCOUNT_CUSTODY_REVENUE, customer_id, credit=economics.custody),
            JournalLeg(
                ACCOUNT_REG_FEES_PAYABLE, customer_id, credit=economics.regulatory
            ),
            JournalLeg(
                economics.broker_payable_account,
                customer_id,
                credit=economics.broker_cost,
            ),
            JournalLeg(
                ACCOUNT_CUSTODIAN_FEES_PAYABLE,
                customer_id,
                credit=economics.custody_cost,
            ),
            JournalLeg(
                ACCOUNT_PARTNER_SHARE_PAYABLE,
                customer_id,
                credit=economics.partner_share,
            ),
        ]

        operations = [
            {
                "type": "set_lot_collection",
                "key": {"customer_id": customer_id, "symbol": symbol},
                "reversible": True,
                "before": [lot.to_dict() for lot in plan.before_lots],
                "after": [lot.to_dict() for lot in plan.after_lots],
            }
        ]
        return legs, operations

    def on_order_partially_filled(self, p: dict, ev: dict, seq: int):
        return self._fill(p, ev, seq, final=False)

    def on_order_filled(self, p: dict, ev: dict, seq: int):
        return self._fill(p, ev, seq, final=True)

    def on_trade_settled(self, p: dict, ev: dict, seq: int):
        trade_id = self._text(p, "trade_id")

        trade = self.trades.get(trade_id)
        if trade is None:
            raise RejectedEvent("unknown_trade", "trade id not found")
        if trade.status != "unsettled":
            raise RejectedEvent("trade_not_unsettled", f"trade is {trade.status}")
        if trade.principal <= ZERO or trade.side not in SIDES:
            raise RejectedEvent("invalid_trade", "stored trade is not settleable")

        if trade.side == "buy":
            legs = [
                JournalLeg(
                    ACCOUNT_UNSETTLED_TRADE_PAYABLE,
                    trade.customer_id,
                    debit=trade.principal,
                ),
                JournalLeg(
                    ACCOUNT_OMNIBUS_CASH, trade.customer_id, credit=trade.principal
                ),
            ]
        else:
            legs = [
                JournalLeg(
                    ACCOUNT_OMNIBUS_CASH, trade.customer_id, debit=trade.principal
                ),
                JournalLeg(
                    ACCOUNT_SETTLEMENT_RECEIVABLE,
                    trade.customer_id,
                    credit=trade.principal,
                ),
            ]

        after = Trade(
            trade_id=trade.trade_id,
            source_event_id=trade.source_event_id,
            order_id=trade.order_id,
            customer_id=trade.customer_id,
            side=trade.side,
            principal=trade.principal,
            status="settled",
        )
        effect = {
            "operations": [
                {
                    "type": "set_trade",
                    "key": trade_id,
                    "reversible": True,
                    "before": trade.to_dict(),
                    "after": after.to_dict(),
                }
            ]
        }
        return legs, effect

    # -- paying it all onward ---------------------------------------------
    def _discharge(self, customer_id: str, payable_account: str):
        outstanding = money(-self.balances[(customer_id, payable_account)])

        if outstanding <= ZERO:
            raise RejectedEvent(
                "nothing_outstanding",
                f"account {payable_account} has nothing outstanding",
            )

        legs = [
            JournalLeg(payable_account, customer_id, debit=outstanding),
            JournalLeg(ACCOUNT_OMNIBUS_CASH, customer_id, credit=outstanding),
        ]
        return legs, dict(NO_EFFECT)

    def on_broker_fees_settled(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        broker = self._text(p, "broker")

        if broker not in TARIFFS:
            raise RejectedEvent("unknown_broker", f"broker {broker!r} is unknown")

        return self._discharge(customer_id, TARIFFS[broker]["payable_account"])

    def on_custodian_fees_settled(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        return self._discharge(customer_id, ACCOUNT_CUSTODIAN_FEES_PAYABLE)

    def on_reg_fees_remitted(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        return self._discharge(customer_id, ACCOUNT_REG_FEES_PAYABLE)

    def on_partner_payout(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        return self._discharge(customer_id, ACCOUNT_PARTNER_SHARE_PAYABLE)

    # -- corporate actions -------------------------------------------------
    def on_dividend_cash(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        self._text(p, "symbol")

        gross = self._amount(p, "gross_amount")
        withholding = self._amount(p, "withholding_tax")
        net = self._amount(p, "net_amount")

        if net < ZERO or gross < ZERO or withholding < ZERO:
            raise RejectedEvent("negative_amount", "dividend amounts must be positive")

        if money(gross - withholding) != net:
            self._warn(
                "dividend_net_mismatch",
                event_type=ev["type"],
                expected=money_str(money(gross - withholding)),
                actual=money_str(net),
            )

        legs = [
            JournalLeg(ACCOUNT_OMNIBUS_CASH, customer_id, debit=net),
            JournalLeg(ACCOUNT_CUSTOMER_WALLET, customer_id, credit=net),
        ]
        return legs, dict(NO_EFFECT)

    def on_dividend_reinvested(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        symbol = self._text(p, "symbol")

        gross = self._amount(p, "gross_amount")
        withholding = self._amount(p, "withholding_tax")
        net = self._positive_amount(p, "net_amount")

        reinvest_price = self._positive_decimal(p, "reinvest_price")
        reinvest_quantity = self._positive_decimal(p, "reinvest_quantity")

        if gross < ZERO or withholding < ZERO:
            raise RejectedEvent("negative_amount", "dividend amounts must be positive")

        if money(gross - withholding) != net:
            self._warn(
                "dividend_net_mismatch",
                event_type=ev["type"],
                expected=money_str(money(gross - withholding)),
                actual=money_str(net),
            )

        if money(reinvest_price * reinvest_quantity) != net:
            self._warn(
                "reinvestment_value_mismatch",
                event_type=ev["type"],
                expected=money_str(money(reinvest_price * reinvest_quantity)),
                actual=money_str(net),
            )

        legs = [
            JournalLeg(ACCOUNT_OMNIBUS_CUSTODY, customer_id, debit=net),
            JournalLeg(ACCOUNT_CUSTOMER_SECURITIES_CLAIM, customer_id, credit=net),
        ]

        lot = Lot(
            lot_id=f"lot:{ev['event_id']}",
            source_event_id=ev["event_id"],
            trade_id=None,
            customer_id=customer_id,
            symbol=symbol,
            quantity=reinvest_quantity,
            total_cost=net,
            created_sequence=seq,
        )
        effect = {
            "operations": [
                {
                    "type": "add_lot",
                    "key": {
                        "customer_id": customer_id,
                        "symbol": symbol,
                        "lot_id": lot.lot_id,
                    },
                    "reversible": True,
                    "before": None,
                    "after": lot.to_dict(),
                }
            ]
        }
        return legs, effect

    def on_stock_split(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        symbol = self._text(p, "symbol")

        ratio_from = self._positive_decimal(p, "ratio_from")
        ratio_to = self._positive_decimal(p, "ratio_to")

        before_lots = self.lots.get((customer_id, symbol), [])
        if not before_lots:
            raise RejectedEvent("no_position", "customer holds nothing in that symbol")

        after_lots = [
            Lot(
                lot_id=lot.lot_id,
                source_event_id=lot.source_event_id,
                trade_id=lot.trade_id,
                customer_id=lot.customer_id,
                symbol=lot.symbol,
                quantity=lot.quantity * ratio_to / ratio_from,
                total_cost=lot.total_cost,
                created_sequence=lot.created_sequence,
            )
            for lot in before_lots
        ]

        effect = {
            "operations": [
                {
                    "type": "set_lot_collection",
                    "key": {"customer_id": customer_id, "symbol": symbol},
                    "reversible": True,
                    "before": [lot.to_dict() for lot in before_lots],
                    "after": [lot.to_dict() for lot in after_lots],
                }
            ]
        }
        return [], effect

    def on_symbol_change(self, p: dict, ev: dict, seq: int):
        customer_id = self._text(p, "customer_id")
        old_symbol = self._text(p, "old_symbol")
        new_symbol = self._text(p, "new_symbol")

        if old_symbol == new_symbol:
            raise RejectedEvent("identical_symbols", "old and new symbols match")

        old_lots = self.lots.get((customer_id, old_symbol), [])
        if not old_lots:
            raise RejectedEvent("no_position", "customer holds nothing in that symbol")

        new_lots = self.lots.get((customer_id, new_symbol), [])

        moved = [
            Lot(
                lot_id=lot.lot_id,
                source_event_id=lot.source_event_id,
                trade_id=lot.trade_id,
                customer_id=lot.customer_id,
                symbol=new_symbol,
                quantity=lot.quantity,
                total_cost=lot.total_cost,
                created_sequence=lot.created_sequence,
            )
            for lot in old_lots
        ]
        combined = sorted(
            list(new_lots) + moved,
            key=lambda lot: lot.created_sequence,
        )

        effect = {
            "operations": [
                {
                    "type": "rename_lots",
                    "key": {"customer_id": customer_id},
                    "reversible": True,
                    "before": {
                        old_symbol: [lot.to_dict() for lot in old_lots],
                        new_symbol: [lot.to_dict() for lot in new_lots],
                    },
                    "after": {
                        old_symbol: [],
                        new_symbol: [lot.to_dict() for lot in combined],
                    },
                }
            ]
        }
        return [], effect

    # -- corrections -------------------------------------------------------
    def on_reversal(self, p: dict, ev: dict, seq: int):
        target_id = self._text(p, "reverses_event_id")

        if target_id == ev["event_id"]:
            raise RejectedEvent("reversal_cycle", "an event cannot reverse itself")

        record = self.event_records.get(target_id)
        if record is None:
            raise RejectedEvent("unknown_reversal_target", "original event not seen")
        if record["status"] != "accepted":
            raise RejectedEvent("reversal_target_rejected", "original event was rejected")
        if target_id in self.reversed_event_ids:
            raise RejectedEvent("already_reversed", "original event is already reversed")

        legs = [
            JournalLeg(
                account=original.account,
                customer_id=original.customer_id,
                debit=original.credit,
                credit=original.debit,
            )
            for original in record["legs"]
        ]

        effect = {
            "operations": [
                {
                    "type": "reverse_event_effect",
                    "reversible": True,
                    "before": {
                        "target_event_id": target_id,
                        "target_was_reversed": False,
                    },
                    "after": {
                        "target_event_id": target_id,
                        "target_was_reversed": True,
                    },
                }
            ]
        }
        return legs, effect

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> dict:
        trial_balance: dict[str, Decimal] = {
            account: ZERO for account in self.accounts_ever_used
        }
        for (_customer_id, account), balance in self.balances.items():
            trial_balance[account] = trial_balance.get(account, ZERO) + balance

        customer_ids: set[str] = {
            customer_id for customer_id, _account in self.balances
        }
        for order in self.orders.values():
            if order.status == "open" and order.placement_seen:
                customer_ids.add(order.customer_id)
        for (customer_id, _symbol), lots in self.lots.items():
            if lots:
                customer_ids.add(customer_id)

        customers: dict[str, dict] = {}
        for customer_id in sorted(customer_ids):
            wallet_cash = money(-self.balances.get((customer_id, "2010"), ZERO))

            cash_hold = money(
                sum(
                    (
                        order.remaining_cash_hold
                        for order in self.orders.values()
                        if order.customer_id == customer_id
                        and order.status == "open"
                        and order.side == "buy"
                    ),
                    ZERO,
                )
            )

            positions: dict[str, dict] = {}
            for (owner, symbol), lots in self.lots.items():
                if owner != customer_id or not lots:
                    continue
                position_quantity = sum((lot.quantity for lot in lots), ZERO)
                cost_basis = money(sum((lot.total_cost for lot in lots), ZERO))
                if position_quantity == ZERO and cost_basis == ZERO:
                    continue
                positions[symbol] = {
                    "quantity": quantity_str(position_quantity),
                    "cost_basis": money_str(cost_basis),
                }

            customers[customer_id] = {
                "wallet_cash": money_str(wallet_cash),
                "cash_hold": money_str(cash_hold),
                "positions": dict(sorted(positions.items())),
            }

        open_order_routes = {
            order.order_id: order.route
            for order in self.orders.values()
            if order.status == "open" and order.placement_seen
        }

        return {
            "trial_balance": {
                account: money_str(balance)
                for account, balance in sorted(trial_balance.items())
            },
            "customers": customers,
            "open_order_routes": dict(sorted(open_order_routes.items())),
        }

    @staticmethod
    def snapshot_as_of(stored_events: list[StoredEvent]) -> dict:
        historical = Book()
        for record in stored_events:
            historical.apply_stored_event(record)
        return historical.snapshot()

    def trial_balance_total(self) -> Decimal:
        return money(sum(self.balances.values(), ZERO))
