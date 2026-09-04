"""Phase 0 capability probe -- can the master act inside a PM's sub-account?

This answers two questions that the rest of the design depends on, and which
cannot be settled by reading documentation:

  1. Can the master SEE and CANCEL an order that a PM placed from their own
     sub-account login?
  2. Can the master CLOSE a position that a PM opened from their own login?

(1) is the one that matters most. If the master cannot cancel a PM's resting
order, then any flatten is unsafe: we sell the stock while their working buy
order quietly refills it, and the "cut" account ends up long again mid-unwind.

Usage:

    python probe.py discover                        # read-only, connects readonly
    python probe.py cancel --account DU123 --perm-id 12345 --arm
    python probe.py cancel --account DU123 --all --arm
    python probe.py close  --account DU123 --symbol AAPL --arm

`discover` is safe and is where you start. The other two refuse to run without
--arm, and refuse regardless against any account not listed in config.toml.
"""

import argparse
import sys
from pathlib import Path

import guards
from evidence import Run

CONFIG = Path(__file__).parent / "config.toml"

CONNECT_TIMEOUT_S = 15
SETTLE_S = 2.5          # let subscriptions populate before reading caches
FILL_WAIT_S = 60        # how long `close` watches an order before reporting


def _ib():
    try:
        from ib_async import IB
        return IB
    except ImportError:
        sys.exit("ib_async is not installed.  /opt/anaconda3/bin/python3 -m pip install ib_async")


def connect(cfg, run, *, readonly: bool, strict_targets: bool = True):
    """Connect to the master session, having passed every guard first.

    strict_targets=False is for `discover` only. Discover is how you find out
    what the account codes ARE, so refusing to run until they are already in
    the config would be a chicken-and-egg trap. It downgrades an unknown
    target to a warning; cancel and close keep the hard refusal.
    """
    gw = cfg.get("gateway", {})
    host, port = gw.get("host", "127.0.0.1"), int(gw.get("port", 0))
    client_id = int(gw.get("client_id", 0))

    guards.assert_paper_port(port)
    declared = cfg.get("accounts", {}).get("targets")
    targets = (declared or []) if not strict_targets else \
        guards.assert_targets_declared(declared)

    IB = _ib()
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, readonly=readonly,
                   timeout=CONNECT_TIMEOUT_S)
    except Exception as e:
        run.log("connect_failed", host=host, port=port,
                client_id=client_id, error=str(e))
        sys.exit(
            f"Could not connect at {host}:{port} (clientId {client_id}) -- {e}\n"
            f"  · Is TWS/Gateway running and logged into the MASTER account?\n"
            f"  · Is the API enabled, and Read-Only API switched OFF?\n"
            f"  · Is clientId {client_id} already in use by another program?\n"
            f"    (clientId 0 is worth fighting for -- see config.example.toml.)"
        )

    managed = list(ib.managedAccounts())
    try:
        warnings = guards.assert_targets_managed(targets, managed)
    except guards.GuardFailure as e:
        if strict_targets:
            ib.disconnect()
            raise
        warnings = [f"{str(e).splitlines()[0]}  (discover continues anyway)"]
        targets = [t for t in targets if t in managed]

    # Only clientId 0 is fed orders placed by hand in TWS. Without this, an
    # order the PM typed manually may simply never appear, and we would draw
    # the wrong conclusion from its absence.
    manual_binding = False
    if client_id == 0:
        try:
            ib.reqAutoOpenOrders(True)
            manual_binding = True
        except Exception as e:
            print(f"  ! could not bind manual TWS orders: {e}")

    run.log("connected", host=host, port=port, client_id=client_id,
            readonly=readonly, managed=managed, targets=targets,
            manual_order_binding=manual_binding)

    print(f"Connected  {host}:{port}  clientId {client_id}  "
          f"({'read-only' if readonly else 'ARMED'})")
    print(f"Managed accounts ({len(managed)}): {', '.join(managed)}")
    shown = ', '.join(targets[:4])
    print(f"Targets ({len(targets)}): {shown}"
          + (f", +{len(targets) - 4} more" if len(targets) > 4 else ""))
    if not manual_binding:
        print("  ! NOT clientId 0 -- manually-placed TWS orders may be invisible.")
        print("    A 'no orders found' result here is NOT evidence of anything.")
    for w in warnings:
        print(f"  ! {w}")
    return ib, targets


# ---------------------------------------------------------------- discover --

def snapshot(ib, targets, run):
    """Read NLV, positions and open orders for the target accounts."""
    ib.reqAccountSummary()
    ib.sleep(SETTLE_S)
    nlv = {av.account: (av.value, av.currency) for av in ib.accountSummary()
           if av.tag == "NetLiquidation"}

    ib.reqPositions()
    ib.sleep(SETTLE_S)
    positions = [p for p in ib.positions() if p.account in targets]

    # reqAllOpenOrders asks for open orders across every client of this login,
    # not just ours. That breadth is the entire point of the probe.
    try:
        ib.reqAllOpenOrders()
    except Exception as e:
        print(f"  ! reqAllOpenOrders failed: {e}")
    ib.sleep(SETTLE_S)
    trades = [t for t in ib.openTrades()
              if (t.order.account or "") in targets or not t.order.account]

    run.log("snapshot",
            nlv={k: v[0] for k, v in nlv.items()},
            positions=[{"account": p.account, "symbol": p.contract.symbol,
                        "secType": p.contract.secType, "conId": p.contract.conId,
                        "currency": p.contract.currency,
                        "position": p.position, "avgCost": p.avgCost}
                       for p in positions],
            open_orders=[{"account": t.order.account, "permId": t.order.permId,
                          "orderId": t.order.orderId, "clientId": t.order.clientId,
                          "action": t.order.action, "qty": t.order.totalQuantity,
                          "type": t.order.orderType, "lmt": t.order.lmtPrice,
                          "symbol": t.contract.symbol, "secType": t.contract.secType,
                          "status": t.orderStatus.status}
                         for t in trades])
    return nlv, positions, trades


def cmd_discover(cfg, run):
    ib, targets = connect(cfg, run, readonly=True, strict_targets=False)
    try:
        # With no usable target list, report on everything visible -- that is
        # the whole reason to run discover the first time.
        scope = targets or list(ib.managedAccounts())
        nlv, positions, trades = snapshot(ib, scope, run)
        targets = scope

        print("\n--- Net liquidation ------------------------------------------")
        for acct in targets:
            value, ccy = nlv.get(acct, ("(not reported)", ""))
            print(f"  {acct}  {value} {ccy}")

        print("\n--- Positions ------------------------------------------------")
        if not positions:
            print("  (none)")
        for p in positions:
            c = p.contract
            print(f"  {p.account}  {c.secType:<5} {c.symbol:<12} "
                  f"{p.position:>12,.2f} @ {p.avgCost:,.4f} {c.currency}  conId={c.conId}")

        print("\n--- Open orders ----------------------------------------------")
        print("  THIS IS THE ANSWER TO QUESTION 1. An order placed from the PM's")
        print("  own login should appear here with their account code.")
        if not trades:
            print("  (none visible)")
        for t in trades:
            o = t.order
            print(f"  {o.account or '(unset)':<10} {t.contract.symbol:<12} "
                  f"{o.action} {o.totalQuantity:>10,.0f} {o.orderType:<6} "
                  f"status={t.orderStatus.status:<12} "
                  f"permId={o.permId} orderId={o.orderId} clientId={o.clientId}")

        print(f"\nEvidence: {run}")
    finally:
        ib.disconnect()


# ------------------------------------------------------------------ cancel --

def cmd_cancel(cfg, run, args):
    ib, targets = connect(cfg, run, readonly=False)
    try:
        _, _, trades = snapshot(ib, targets, run)
        if args.account not in targets:
            sys.exit(f"REFUSING: {args.account} is not in [accounts].targets.")

        # permId is the identifier to match on: orderId is scoped to whichever
        # client created the order, so a PM's manual order has an orderId from
        # a namespace we do not control.
        wanted = [t for t in trades if (t.order.account or "") == args.account
                  and (args.all or t.order.permId == args.perm_id
                       or t.order.orderId == args.order_id)]
        if not wanted:
            sys.exit(f"No matching open order found in {args.account}. "
                     f"Run `discover` first and check what is actually visible.")

        print(f"\nWould cancel {len(wanted)} order(s) in {args.account}:")
        for t in wanted:
            print(f"  {t.contract.symbol} {t.order.action} {t.order.totalQuantity} "
                  f"permId={t.order.permId} status={t.orderStatus.status}")
        guards.assert_armed(args.arm, "cancelling orders")

        for t in wanted:
            before = t.orderStatus.status
            run.log("cancel_submitted", account=args.account,
                    permId=t.order.permId, symbol=t.contract.symbol, before=before)
            ib.cancelOrder(t.order)

        ib.sleep(SETTLE_S * 2)
        print("\n--- Result ---------------------------------------------------")
        for t in wanted:
            after = t.orderStatus.status
            ok = after in ("Cancelled", "ApiCancelled", "PendingCancel")
            print(f"  {'OK ' if ok else 'XX '} permId={t.order.permId} "
                  f"{t.contract.symbol}  status={after}")
            run.log("cancel_result", permId=t.order.permId, status=after,
                    cancelled=ok)
        print(f"\nEvidence: {run}")
    finally:
        ib.disconnect()


# ------------------------------------------------------------------- close --

class ContractError(RuntimeError):
    """A position we cannot build a routable order for."""


def fmt_qty(q: float) -> str:
    """Crypto positions are fractional to 8dp; equities are whole numbers.
    Formatting both at 2dp prints a real 0.00018171 BTC position as "0.00",
    which is exactly the kind of thing you do not want a dry run to say."""
    return f"{q:,.8f}".rstrip("0").rstrip(".") if q != int(q) else f"{q:,.0f}"


_ROUTE_CACHE: dict[int, object] = {}


def resolve_contract(ib, pos):
    """Turn a Position's stub contract into something that will actually route.

    Qualifying on conId ALONE is the reliable move: IBKR fills in the correct
    venue itself -- CME for a future, PAXOS for crypto, SEHK for a Hong Kong
    listing, SMART where SMART genuinely applies. Passing an exchange in is
    what breaks things, because the exchange on a Position is often blank, and
    guessing SMART is wrong for every non-US equity, every future and all
    crypto. Those all fail with "No security definition", the order is
    rejected, and a naive dry run cheerfully reports the position as closeable.

    qualifyContracts also returns [None] rather than raising when it cannot
    resolve something, so the result has to be checked.
    """
    from ib_async import Contract

    src = pos.contract
    if src.conId in _ROUTE_CACHE:
        return _ROUTE_CACHE[src.conId]

    qualified = [c for c in ib.qualifyContracts(Contract(conId=src.conId))
                 if c is not None]
    if not qualified and src.exchange:
        qualified = [c for c in ib.qualifyContracts(
            Contract(conId=src.conId, exchange=src.exchange)) if c is not None]
    if not qualified:
        raise ContractError(f"could not resolve {src.secType} {src.symbol} "
                            f"(conId={src.conId}) on any venue")

    c = qualified[0]
    if not ib.reqContractDetails(c):
        raise ContractError(
            f"{c.secType} {c.symbol} does not resolve on {c.exchange!r} -- "
            f"an order here would be rejected")
    _ROUTE_CACHE[src.conId] = c
    return c


def cmd_close(cfg, run, args):
    from ib_async import LimitOrder, MarketOrder

    ib, targets = connect(cfg, run, readonly=False)
    try:
        _, positions, _ = snapshot(ib, targets, run)
        if args.account not in targets:
            sys.exit(f"REFUSING: {args.account} is not in [accounts].targets.")

        matches = [p for p in positions if p.account == args.account
                   and (p.contract.conId == args.con_id
                        or p.contract.symbol == args.symbol) and p.position]
        if len(matches) != 1:
            sys.exit(f"Need exactly one matching position, found {len(matches)}. "
                     f"Phase 0 closes one position at a time on purpose -- "
                     f"disambiguate with --con-id.")
        pos = matches[0]

        try:
            contract = resolve_contract(ib, pos)
        except ContractError as e:
            sys.exit(f"REFUSING: {e}")

        action = "SELL" if pos.position > 0 else "BUY"
        qty = abs(pos.position)
        order = (LimitOrder(action, qty, args.limit_price)
                 if args.limit_price is not None else MarketOrder(action, qty))
        order.account = args.account      # <- the whole mechanism, one line
        # IBKR will not accept a DAY market order in crypto; it has to be IOC.
        order.tif = "IOC" if (contract.secType == "CRYPTO"
                              and args.limit_price is None) else "DAY"

        print(f"\nWould close in {args.account}:")
        print(f"  {action} {fmt_qty(qty)} {contract.localSymbol or contract.symbol} "
              f"({contract.secType} on {contract.exchange}) "
              f"as {'LMT @ ' + str(args.limit_price) if args.limit_price else 'MKT'} "
              f"{order.tif}")
        guards.assert_armed(args.arm, "closing a position")

        run.log("close_submitted", account=args.account, action=action,
                qty=qty, conId=contract.conId, symbol=contract.symbol,
                orderType=order.orderType, limit=args.limit_price)

        trade = ib.placeOrder(contract, order)
        print("\n--- Order progress -------------------------------------------")
        seen, waited = None, 0.0
        while waited < FILL_WAIT_S:
            ib.sleep(1.0)
            waited += 1.0
            st = trade.orderStatus
            if st.status != seen:
                seen = st.status
                print(f"  [{waited:5.0f}s] {st.status:<14} "
                      f"filled={fmt_qty(st.filled)} remaining={fmt_qty(st.remaining)} "
                      f"avgPrice={st.avgFillPrice}")
                run.log("order_status", permId=trade.order.permId,
                        status=st.status, filled=st.filled,
                        remaining=st.remaining, avgFillPrice=st.avgFillPrice)
            if st.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break

        for f in trade.fills:
            run.log("fill", permId=trade.order.permId, execId=f.execution.execId,
                    account=f.execution.acctNumber, shares=f.execution.shares,
                    price=f.execution.price, time=f.execution.time)
        if trade.log:
            for entry in trade.log:
                run.log("order_log", status=entry.status, message=entry.message)

        # Re-read positions: the point is to prove the fill landed in the SUB,
        # not in the master. Anything else means the whole design is wrong.
        ib.sleep(SETTLE_S)
        ib.reqPositions()
        ib.sleep(SETTLE_S)
        after = [p for p in ib.positions() if p.contract.conId == contract.conId]
        print("\n--- Position after -------------------------------------------")
        if not after:
            print(f"  flat in every account")
        for p in after:
            print(f"  {p.account}  {p.contract.symbol}  {fmt_qty(p.position)}")
        run.log("positions_after",
                rows=[{"account": p.account, "conId": p.contract.conId,
                       "position": p.position} for p in after])
        print(f"\nEvidence: {run}")
    finally:
        ib.disconnect()


# -------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="read-only: accounts, NLV, positions, open orders")

    c = sub.add_parser("cancel", help="cancel an order in a sub-account")
    c.add_argument("--account", required=True)
    c.add_argument("--perm-id", type=int)
    c.add_argument("--order-id", type=int)
    c.add_argument("--all", action="store_true", help="every open order in that account")
    c.add_argument("--arm", action="store_true")

    x = sub.add_parser("close", help="close one position in a sub-account")
    x.add_argument("--account", required=True)
    x.add_argument("--symbol")
    x.add_argument("--con-id", type=int)
    x.add_argument("--limit-price", type=float,
                   help="omit for a market order (fine for a liquid paper canary, "
                        "never for a wide options book)")
    x.add_argument("--arm", action="store_true")

    args = ap.parse_args()
    run = Run(args.cmd)
    try:
        cfg = guards.load_config(CONFIG)
        if args.cmd == "discover":
            cmd_discover(cfg, run)
        elif args.cmd == "cancel":
            if not (args.all or args.perm_id or args.order_id):
                sys.exit("Give --perm-id, --order-id, or --all.")
            cmd_cancel(cfg, run, args)
        else:
            if not (args.symbol or args.con_id):
                sys.exit("Give --symbol or --con-id.")
            cmd_close(cfg, run, args)
    except guards.GuardFailure as e:
        run.log("guard_failure", message=str(e))
        sys.exit(f"\n{e}\n")


if __name__ == "__main__":
    main()
