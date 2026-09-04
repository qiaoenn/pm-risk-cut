"""The cut: cancel everything working, then flatten the book in risk order.

Two rules shape this file.

  1. Cancel before you sell. If a PM has a resting buy and we start selling,
     their order refills the position underneath us and the account ends the
     "cut" still long. Cancellation is proven to work from the master (Phase 0),
     and it is step one, always.

  2. Unwind by unboundedness, not by P&L. Closing only the losing legs of a
     derivatives book can leave a naked short option or an unhedged leg -- a
     risk control that increases risk. Short options go first, then short
     equities, then futures, then long options, then everything else.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from probe import ContractError, resolve_contract, fmt_qty

# Lower number = closed earlier. The ordering is the safety property.
PRIORITY_UNBOUNDED_SHORT_OPTION = 0
PRIORITY_UNBOUNDED_SHORT = 1
PRIORITY_LEVERAGED = 2
PRIORITY_DECAYING_LONG = 3
PRIORITY_PLAIN_LONG = 4


def priority(position) -> int:
    sec, qty = position.contract.secType, position.position
    if sec in ("OPT", "FOP"):
        return PRIORITY_UNBOUNDED_SHORT_OPTION if qty < 0 else PRIORITY_DECAYING_LONG
    if qty < 0 and sec in ("STK", "CFD"):
        return PRIORITY_UNBOUNDED_SHORT
    if sec in ("FUT", "FOP"):
        return PRIORITY_LEVERAGED
    return PRIORITY_PLAIN_LONG


def tradable_now(ib, contract) -> tuple[bool, str]:
    """Is this instrument's market open right now?

    Uses contract details rather than market data, deliberately -- trading
    hours come back without any market data subscription, which this setup
    does not have. Unknown hours are treated as tradable: better to try and
    get a clean reject than to skip a position we could have closed.
    """
    try:
        details = ib.reqContractDetails(contract)
        if not details:
            return False, "no contract details -- unroutable, not attempted"
        d = details[0]
        hours, tz = (d.liquidHours or d.tradingHours or ""), d.timeZoneId
        if not hours or not tz:
            return True, "no trading hours published; attempting anyway"
        now = datetime.now(ZoneInfo(tz))
        for span in hours.split(";"):
            if not span or "CLOSED" in span:
                continue
            start, _, end = span.partition("-")
            try:
                s = datetime.strptime(start, "%Y%m%d:%H%M").replace(tzinfo=ZoneInfo(tz))
                e = datetime.strptime(end, "%Y%m%d:%H%M").replace(tzinfo=ZoneInfo(tz))
            except ValueError:
                continue
            if s <= now <= e:
                return True, f"open ({tz})"
        return False, f"closed ({tz}); deferred to next session"
    except Exception as e:
        return True, f"hours check failed ({e}); attempting anyway"


def _sanity(contract, action: str, qty: float, position: float) -> str | None:
    """Replaces the TWS Order Precautions we had to bypass for headless use.

    Bypassing those precautions is mandatory (a modal dialog hangs API orders
    forever on a machine with no operator) but it removes the only thing
    standing between a bug and an absurd order. So the checks live here.
    """
    if qty <= 0:
        return f"refusing non-positive quantity {qty}"
    if abs(qty - abs(position)) > 1e-9:
        return (f"quantity {qty} does not match position {abs(position)} -- "
                f"a close may never be larger than the position it closes")
    if (position > 0 and action != "SELL") or (position < 0 and action != "BUY"):
        return f"action {action} would increase a position of {position}"
    return None


def cancel_all_orders(ib, account: str, run, passes: int = 3) -> list:
    """Cancel every working order in the account, repeatedly.

    Repeatedly because a PM may still be submitting while we cut. The account
    should be locked before this runs, but the loop is cheap insurance.
    """
    cancelled = []
    for attempt in range(passes):
        ib.reqAllOpenOrders()
        ib.sleep(2)
        live = [t for t in ib.openTrades() if (t.order.account or "") == account]
        if not live:
            break
        for t in live:
            run.log("cancel_submitted", account=account, permId=t.order.permId,
                    symbol=t.contract.symbol, attempt=attempt)
            ib.cancelOrder(t.order)
            cancelled.append(t)
        ib.sleep(3)
    return cancelled


def working_orders(ib, account: str) -> list:
    ib.reqAllOpenOrders()
    ib.sleep(2)
    return [t for t in ib.openTrades() if (t.order.account or "") == account]


def open_positions(ib, account: str) -> list:
    ib.reqPositions()
    ib.sleep(2)
    # Zero-quantity rows are real in IBKR's feed (settled crypto especially)
    # and must not become orders.
    return [p for p in ib.positions()
            if p.account == account and abs(p.position) > 1e-9]


def flatten(ib, account: str, run, *, dry_run: bool = True,
            wait_s: int = 120) -> dict:
    """Cancel, then close every position in risk order. Returns a report."""
    from ib_async import MarketOrder

    report = {"account": account, "dry_run": dry_run, "cancelled": 0,
              "closed": [], "deferred": [], "failed": [], "residual": [],
              # permIds of the orders THIS cut placed. Reconciliation must sum
              # only these: ib.trades() and reqExecutions both return the whole
              # day, so a PM's earlier buys would otherwise net against our
              # sells and the tie-out would come out near zero -- looking like
              # nothing happened when in fact the book was fully liquidated.
              "perm_ids": []}

    if not dry_run:
        report["cancelled"] = len(cancel_all_orders(ib, account, run))

        # Crash-recovery guard. If this process died between submitting a close
        # and the fill landing, that order is still working at IBKR -- IBKR
        # does not care that we restarted. Positions may not yet reflect it, so
        # naively re-enumerating and resubmitting would sell the same holding
        # twice and leave the account SHORT: a risk control creating a new
        # position out of nothing.
        #
        # Cancelling first (above) then re-reading positions handles the normal
        # case. This refuses to continue if anything is STILL working after
        # cancellation, because at that point we cannot know what quantity is
        # genuinely open. The next sweep retries.
        stale = working_orders(ib, account)
        if stale:
            report["failed"].append({
                "instrument": "(pre-flight)",
                "reason": f"{len(stale)} order(s) still working after "
                          f"cancellation -- refusing to submit duplicates, "
                          f"which could leave the account short"})
            run.log("aborted_orders_still_working", account=account,
                    permIds=[t.order.permId for t in stale])
            return report

    positions = sorted(open_positions(ib, account), key=priority)
    trades, submitted = [], []
    for pos in positions:
        c = pos.contract
        tag = f"{c.secType} {c.symbol}"
        try:
            contract = resolve_contract(ib, pos)
        except ContractError as e:
            # Unroutable is a FAILURE, never a silent pass. A cut that reports
            # success while placing nothing is the worst outcome available.
            report["failed"].append({"instrument": tag, "reason": str(e)})
            continue

        ok, why = tradable_now(ib, contract)
        if not ok:
            report["deferred"].append({"instrument": tag, "reason": why,
                                       "qty": pos.position})
            continue

        action = "SELL" if pos.position > 0 else "BUY"
        qty = abs(pos.position)
        problem = _sanity(contract, action, qty, pos.position)
        if problem:
            report["failed"].append({"instrument": tag, "reason": problem})
            continue

        if dry_run:
            report["closed"].append({"instrument": tag, "action": action,
                                     "qty": qty, "priority": priority(pos)})
            continue

        order = MarketOrder(action, qty)
        order.account = account
        order.tif = "IOC" if contract.secType == "CRYPTO" else "DAY"
        run.log("close_submitted", account=account, instrument=tag,
                action=action, qty=qty, priority=priority(pos))
        trade = ib.placeOrder(contract, order)
        trades.append((tag, trade))
        submitted.append(trade)
        ib.sleep(0.4)          # stay well inside IBKR's ~50 msg/s pacing

    # Submit-then-watch, rather than blocking on each order: a short option and
    # a short stock should both be working while we wait, not queued behind
    # each other.
    waited = 0.0
    while trades and waited < wait_s:
        ib.sleep(2.0)
        waited += 2.0
        for tag, t in list(trades):
            st = t.orderStatus
            if st.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                entry = {"instrument": tag, "status": st.status,
                         "filled": st.filled, "remaining": st.remaining,
                         "avgFillPrice": st.avgFillPrice,
                         "permId": t.order.permId}
                run.log("close_result", account=account, **entry)
                (report["closed"] if st.status == "Filled"
                 else report["residual"]).append(entry)
                trades.remove((tag, t))

    for tag, t in trades:          # still working when the clock ran out
        st = t.orderStatus
        entry = {"instrument": tag, "status": f"TIMEOUT/{st.status}",
                 "filled": st.filled, "remaining": st.remaining,
                 "permId": t.order.permId}
        run.log("close_timeout", account=account, **entry)
        report["residual"].append(entry)

    report["perm_ids"] = [t.order.permId for t in submitted if t.order.permId]
    run.log("flatten_report", **report)
    return report


def render(report: dict) -> str:
    r, out = report, []
    out.append(f"  account       {r['account']}"
               + ("   [DRY RUN]" if r["dry_run"] else ""))
    out.append(f"  orders cancelled  {r['cancelled']}")
    for label, key in (("closed", "closed"), ("deferred", "deferred"),
                       ("residual", "residual"), ("FAILED", "failed")):
        rows = r[key]
        out.append(f"  {label} ({len(rows)})")
        for x in rows:
            detail = x.get("reason") or (
                f"{x['status']} filled={fmt_qty(x.get('filled') or 0)}"
                if "status" in x else "would submit")
            qty = x.get("qty")
            out.append(f"     {x['instrument']:<22} "
                       f"{(x.get('action','') + ' ' + fmt_qty(qty)) if qty else ''} {detail}")
    return "\n".join(out)
