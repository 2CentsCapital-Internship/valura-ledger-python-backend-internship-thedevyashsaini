"""Ledger Arena - interview mode.
 
Run this after you make the change we asked for. It streams a small, fixed set
of events into your book and tells you whether the change took effect.
 
    python interview.py --interview --task 2
    python interview.py --interview --task 2 --repo /path/to/your/repo
    python interview.py --interview --all          # report every task it can see
 
No network, no arena attempt is used, nothing is uploaded. It imports your
`book.py` from the current directory (or --repo) and calls the same two methods
the arena client calls: Book.apply(event) and Book.snapshot().
"""
from __future__ import annotations
 
import argparse
import importlib.util
import json
import os
import sys
from decimal import Decimal, InvalidOperation
 
D = Decimal
 
 
# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
def ev(n, typ, payload, note=""):
    return {"offset": n, "event_id": f"evt_iv_{n:04d}", "type": typ,
            "payload": payload, "_note": note}
 
 
C = "CUST-9001"
 
 
def fill(n, side, qty, price, principal, broker="BRK-A", sym="ACME",
         order="ORD-1", trade="TRD-1", note="", eid=None):
    e = ev(n, "order_filled", {
        "order_id": order, "customer_id": C, "side": side, "symbol": sym,
        "quantity": str(qty), "price": str(price), "principal": str(principal),
        "asset_class": "equity", "broker": broker, "partner_rate": "0.50",
        "trade_id": trade}, note)
    if eid:
        e["event_id"] = eid
    return e
 
 
DEPOSIT = ev(1, "deposit", {"customer_id": C, "amount": "5000.00"}, "fund")
BUY_A = fill(2, "buy", 3, "33.333333", "100.00", order="ORD-A", trade="TRD-A",
             note="buy 3 @ principal 100.00 (per-share does not divide)")
BUY_B = fill(3, "buy", 2, "25.00", "50.00", order="ORD-B", trade="TRD-B",
             note="buy 2 @ principal 50.00")
SELL_2 = fill(4, "sell", 2, "60.00", "120.00", order="ORD-C", trade="TRD-C",
              note="sell 2 - relieves part of the 3-share lot")
 
LOT_EVENTS = [DEPOSIT, BUY_A, BUY_B, SELL_2]
 
OVERSELL = [DEPOSIT, BUY_A, BUY_B,
            fill(4, "sell", 50, "60.00", "3000.00", order="ORD-D",
                 trade="TRD-D", note="OVERSELL: 50 against 5 held")]
 
DUP = [DEPOSIT, BUY_A, BUY_B, SELL_2,
       fill(5, "sell", 2, "60.00", "120.00", order="ORD-C", trade="TRD-C",
            eid="evt_iv_DIFFERENT",
            note="same trade_id as the sell, brand new event_id")]
 
LOSS = [DEPOSIT,
        fill(2, "buy", 4, "25.00", "100.00", broker="BRK-B", sym="ZINC",
             order="ORD-L", trade="TRD-L",
             note="loss-making fill: BRK-B ticket 3.00 > revenue 2.55")]
 
REBATE = [DEPOSIT, BUY_A,
          ev(3, "fee_rebate", {"customer_id": C, "amount": "1.00"},
             "new event type: firm refunds 1.00 of brokerage")]
 
PNL = [DEPOSIT, BUY_A, BUY_B, SELL_2,
       fill(5, "sell", 3, "40.00", "120.00", order="ORD-E", trade="TRD-E",
            note="sell the rest")]
 
 
# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
def load_book(repo):
    hits = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", ".venv", "venv", "node_modules")]
        if "book.py" in files:
            hits.append(os.path.join(root, "book.py"))
    if not hits:
        sys.exit(f"interview: no book.py found under {repo}")
    path = sorted(hits, key=lambda p: p.count(os.sep))[0]
    sys.path.insert(0, os.path.dirname(path))
    sys.path.insert(0, repo)
    spec = importlib.util.spec_from_file_location("book", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["book"] = mod          # dataclasses need this before exec
    spec.loader.exec_module(mod)
    return mod, path
 
 
def run(mod, events):
    book = mod.Book()
    out = []
    for e in events:
        try:
            legs = book.apply(e) or []
        except Exception as exc:                                # noqa: BLE001
            out.append(("RAISED", f"{type(exc).__name__}: {exc}", []))
            continue
        out.append(("ok", e, legs))
    return book, out
 
 
def amt(legs, account, side):
    t = D("0")
    for l in legs or []:
        if str(l.get("account")) != account:
            continue
        try:
            t += D(str(l.get(side, "0") or "0"))
        except InvalidOperation:
            pass
    return t
 
 
def last_legs(res):
    for tag, e, legs in reversed(res):
        if tag == "ok":
            return legs
    return []
 
 
# --------------------------------------------------------------------------
# the eight checks
# --------------------------------------------------------------------------
def t1(mod):
    """Partial-lot rounding: round per share instead of once at lot level."""
    _, res = run(mod, LOT_EVENTS)
    got = amt(last_legs(res), "1200", "credit")
    return (got == D("66.66"), f"cost relieved on the sell = {got}",
            "expected 66.66 (per-share: 100.00/3 = 33.33 each). "
            "66.67 means it is still rounding once at lot level.")
 
 
def t2(mod):
    """Regulatory fee 8 bps -> 10 bps."""
    _, res = run(mod, [DEPOSIT, BUY_A])
    legs = last_legs(res)
    fee, wallet = amt(legs, "2400", "credit"), amt(legs, "2010", "debit")
    
    print(legs, fee, wallet)
    return (fee == D("0.10"),
            f"regulatory fee = {fee}, customer wallet debit = {wallet}",
            "expected fee 0.10 and wallet 101.14. If the wallet is still "
            "101.12 the customer was not charged the new fee.")
 
 
def t3(mod):
    """FIFO -> LIFO."""
    _, res = run(mod, LOT_EVENTS)
    got = amt(last_legs(res), "1200", "credit")
    return (got == D("50.00"), f"cost relieved on the sell = {got}",
            "expected 50.00 (LIFO takes the whole 2-share lot costing 50.00). "
            "66.67 is still FIFO.")
 
 
def t4(mod):
    """Partial oversell: fill what is held, reject the remainder."""
    _, res = run(mod, OVERSELL)
    legs = last_legs(res)
    relieved = amt(legs, "1200", "credit")
    return (bool(legs) and relieved > 0,
            f"legs posted = {len(legs)}, cost relieved = {relieved}",
            "expected the 5 held shares to be sold and the rest refused. "
            "No legs means it still rejects the whole event.")
 
 
def t5(mod):
    """Same trade_id, fresh event_id, must not post twice."""
    _, res = run(mod, DUP)
    legs = last_legs(res)
    return (not legs, f"legs posted for the duplicate = {len(legs)}",
            "expected none. Any legs here means the trade was booked twice.")
 
 
def t6(mod):
    """Realised P&L reported in snapshot()."""
    book, _ = run(mod, PNL)
    try:
        snap = book.snapshot()
    except Exception as exc:                                    # noqa: BLE001
        return (False, f"snapshot() raised {type(exc).__name__}: {exc}", "")
    blob = json.dumps(snap, default=str).lower()
    keys = [k for k in ("realised", "realized", "pnl", "p_and_l")
            if k in blob]
    return (bool(keys), f"snapshot keys matching realised P&L: {keys or 'none'}",
            "expected a realised-P&L figure per customer after two sells.")
 
 
def t7(mod):
    """New event type: fee_rebate."""
    _, res = run(mod, REBATE)
    legs = last_legs(res)
    dr4000, cr2010 = amt(legs, "4000", "debit"), amt(legs, "2010", "credit")
    return (dr4000 == D("1.00") and cr2010 == D("1.00"),
            f"4000 debit = {dr4000}, 2010 credit = {cr2010}",
            "expected brokerage revenue 4000 debited 1.00 and the customer "
            "wallet 2010 credited 1.00.")
 
 
def t8(mod):
    """Partner share: remove the zero floor so losses are shared."""
    _, res = run(mod, LOSS)
    legs = last_legs(res)
    dr, cr = amt(legs, "5100", "debit"), amt(legs, "5100", "credit")
    return (cr > 0 or dr < 0,
            f"partner share leg 5100: debit {dr}, credit {cr}",
            "this fill loses money (revenue 2.55, cost 3.11). Expected the "
            "partner to share the loss - a credit on 5100 - instead of the "
            "leg being absent.")
 
 
TASKS = {
    1: ("Partial-lot rounding", t1),
    2: ("Regulatory fee 8 bps -> 10 bps", t2),
    3: ("FIFO -> LIFO", t3),
    4: ("Partial oversell", t4),
    5: ("Duplicate trade, fresh event id", t5),
    6: ("Realised P&L in snapshot()", t6),
    7: ("New event type: fee_rebate", t7),
    8: ("Remove the partner-share floor", t8),
}
 
 
def main(argv=None):
    ap = argparse.ArgumentParser(prog="interview")
    ap.add_argument("--interview", action="store_true",
                    help="run in interview mode (required)")
    ap.add_argument("--task", type=int, choices=sorted(TASKS))
    ap.add_argument("--all", action="store_true", help="report every task")
    ap.add_argument("--repo", default=".", help="path to your repo")
    a = ap.parse_args(argv)
 
    if not a.interview:
        ap.error("pass --interview")
    if not a.task and not a.all:
        ap.error("pass --task N (1-8) or --all")
 
    mod, path = load_book(os.path.abspath(a.repo))
    print(f"interview mode - loaded {path}\n")
 
    todo = sorted(TASKS) if a.all else [a.task]
    passed = 0
    for n in todo:
        title, fn = TASKS[n]
        try:
            ok, observed, expected = fn(mod)
        except Exception as exc:                                # noqa: BLE001
            ok, observed, expected = False, f"check crashed: {exc}", ""
        mark = "PASS" if ok else "not yet"
        passed += bool(ok)
        print(f"[{mark:>7}]  Task {n}: {title}")
        print(f"           observed: {observed}")
        if not ok and expected:
            print(f"           {expected}")
        print()
 
    if a.all:
        print(f"{passed} of {len(todo)} tasks show the change applied.")
        print("Tasks 1 and 3 are alternative conventions - you would not "
              "normally have both.")
    return 0 if (a.all or passed) else 1
 
 
if __name__ == "__main__":
    sys.exit(main())