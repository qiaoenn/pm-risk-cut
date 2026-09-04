"""Operator CLI for the PM drawdown cut.

    python riskctl.py enroll --all --baseline current
    python riskctl.py enroll --account DUQ782853 --baseline 1000000
    python riskctl.py status
    python riskctl.py cut --account DUQ782853            # dry run
    python riskctl.py cut --account DUQ782853 --arm      # for real
    python riskctl.py watch                              # detect + report only
    python riskctl.py watch --arm                        # detect + CUT
    python riskctl.py reopen --account DUQ782853 --baseline 950000

Nothing that changes an account runs without --arm.
"""

import argparse
import sys
import time
from pathlib import Path

import cut_engine
import guards
import monitor
import probe
import reconcile
import state
from evidence import Run

CONFIG = Path(__file__).parent / "config.toml"


def _connect(cfg, run, readonly):
    return probe.connect(cfg, run, readonly=readonly, strict_targets=True)


def cmd_enroll(cfg, run, args):
    ib, targets = _connect(cfg, run, readonly=True)
    try:
        nlv = monitor.read_nlv(ib)
        accounts = targets if args.all else [args.account]
        for acct in accounts:
            if acct not in nlv:
                print(f"  skip {acct}: no NLV reported")
                continue
            baseline = nlv[acct] if args.baseline == "current" else float(args.baseline)
            state.enroll(acct, baseline, note=args.note or "")
            print(f"  {acct}  baseline {baseline:,.2f}  "
                  f"floor {baseline * (1 - float(cfg['risk']['drawdown_pct'])):,.2f}")
    finally:
        ib.disconnect()


def cmd_status(cfg, run, args):
    ib, _ = _connect(cfg, run, readonly=True)
    try:
        rows = monitor.evaluate(ib, cfg)
        if not rows:
            sys.exit("No accounts enrolled. Run `riskctl.py enroll --all "
                     "--baseline current` first.")
        print(monitor.render(rows))
    finally:
        ib.disconnect()


def _do_cut(ib, acct, run, *, dry_run):
    before = reconcile.snapshot(ib, acct)
    run.log("snapshot_before", **before)
    if not dry_run:
        state.set_status(acct, state.CUTTING, "cut started")

    report = cut_engine.flatten(ib, acct, run, dry_run=dry_run)
    print(cut_engine.render(report))

    if dry_run:
        return report

    after = reconcile.snapshot(ib, acct)
    run.log("snapshot_after", **after)
    # Only this cut's own fills. ib.trades() carries the whole session, so
    # filtering by account alone would net the PM's earlier buys against our
    # sells and report a liquidated book as "nothing moved".
    mine = set(report.get("perm_ids") or [])
    fills = [{"shares": f.execution.shares, "price": f.execution.price,
              "side": f.execution.side,
              "commission": getattr(f.commissionReport, "commission", 0.0)}
             for t in ib.trades()
             if (t.order.account or "") == acct and t.order.permId in mine
             for f in t.fills]
    rec = reconcile.compare(before, after, fills)
    run.log("reconciliation", **rec)
    print("\n--- Reconciliation -------------------------------------------")
    print(reconcile.render(rec))

    state.set_status(acct, state.LOCKED,
                     f"cut complete; {len(rec['still_open'])} residual")
    print(f"\n  {acct} is now LOCKED. Reopen with:")
    print(f"     python riskctl.py reopen --account {acct} --baseline <new capital>")
    return report


def cmd_cut(cfg, run, args):
    ib, targets = _connect(cfg, run, readonly=not args.arm)
    try:
        if args.account not in targets:
            sys.exit(f"REFUSING: {args.account} is not in [accounts].targets.")
        if not state.get(args.account):
            sys.exit(f"REFUSING: {args.account} is not enrolled -- no baseline, "
                     f"so no floor to justify a cut.")
        print(f"\n--- Cut {args.account} "
              f"{'(DRY RUN)' if not args.arm else '(ARMED)'} ---")
        _do_cut(ib, args.account, run, dry_run=not args.arm)
        if not args.arm:
            print("\n  Dry run only. Re-run with --arm to place orders.")
        print(f"\nEvidence: {run}")
    finally:
        ib.disconnect()


def cmd_watch(cfg, run, args):
    risk = cfg.get("risk", {})
    poll = float(risk.get("poll_seconds", 15))
    needed = int(risk.get("confirm_samples", 3))
    max_jump = float(risk.get("max_jump_pct", 0.02))
    police_every = float(risk.get("police_seconds", 300))

    ib, targets = _connect(cfg, run, readonly=not args.arm)
    streak, last_nlv, last_police = {}, {}, {}
    print(f"Watching {len(state.all_accounts())} enrolled account(s) every "
          f"{poll:.0f}s. {'ARMED -- will cut.' if args.arm else 'Detect-only.'}")
    print("Ctrl-C to stop.\n")
    cycles = 0
    try:
        while args.max_cycles is None or cycles < int(args.max_cycles):
            cycles += 1
            for r in monitor.evaluate(ib, cfg):
                acct, lvl = r["account"], r["level"]
                if r["status"] == state.CUTTING:
                    continue

                # A locked account is not finished business. Two jobs, one
                # mechanism: positions deferred because their market was shut
                # still have to be closed when it opens, and a PM can simply
                # buy back in -- which nothing else prevents, because the
                # server-side restriction (Layer 1) is unavailable on this
                # account structure.
                if r["status"] == state.LOCKED:
                    if args.arm and (time.monotonic()
                                     - last_police.get(acct, 0.0)) >= police_every:
                        last_police[acct] = time.monotonic()
                        rep = cut_engine.flatten(ib, acct, run, dry_run=False)
                        if rep["closed"] or rep["cancelled"]:
                            print(f"  police {acct}: cancelled "
                                  f"{rep['cancelled']}, closed "
                                  f"{len(rep['closed'])}")
                        if rep["deferred"]:
                            print(f"  police {acct}: {len(rep['deferred'])} "
                                  f"still awaiting their market")
                    continue

                prev = last_nlv.get(acct)
                if monitor.implausible(prev, r["nlv"], max_jump):
                    run.log("implausible_nlv", account=acct, prev=prev,
                            now=r["nlv"])
                    print(f"  ! {acct} NLV {prev:,.0f} -> {r['nlv']:,.0f} in one "
                          f"poll. Treating as bad data, not a loss.")
                    last_nlv[acct] = r["nlv"]
                    continue
                last_nlv[acct] = r["nlv"]

                if lvl == monitor.WARN and r["status"] != state.WARNED:
                    state.set_status(acct, state.WARNED, f"dd={r['drawdown']:.2%}")
                    run.log("warn", **r)
                    print(f"  WARN {acct} {r['drawdown']:.2%} "
                          f"(headroom {r['headroom']:,.0f})")

                if monitor.confirm(streak, acct, lvl, needed):
                    run.log("breach_confirmed", **r)
                    print(f"\n  *** BREACH {acct} NLV {r['nlv']:,.2f} <= floor "
                          f"{r['floor']:,.2f} ({r['drawdown']:.2%}) ***")
                    if args.arm:
                        _do_cut(ib, acct, run, dry_run=False)
                    else:
                        print("  detect-only: no orders placed. "
                              "Re-run `watch --arm` to cut automatically.")
                        state.set_status(acct, state.WARNED, "breach, not armed")
                elif lvl == monitor.BREACH:
                    print(f"  breach {acct} sample "
                          f"{streak.get(acct, 0)}/{needed} (awaiting confirmation)")
            state.beat(f"{len(state.all_accounts())} accounts, "
                       f"{'armed' if args.arm else 'detect-only'}")
            ib.sleep(poll)
    except KeyboardInterrupt:
        print("\nstopped.")
    else:
        print(f"\nstopped after {cycles} cycle(s).")
    finally:
        ib.disconnect()


def cmd_health(cfg, run, args):
    """Is the watchdog actually alive? Exit 1 if not, so cron can alert."""
    from datetime import datetime, timezone
    ts, detail = state.last_beat()
    if not ts:
        sys.exit("NO HEARTBEAT EVER RECORDED -- watch has never run.")
    age = (datetime.now(timezone.utc)
           - datetime.fromisoformat(ts)).total_seconds()
    limit = float(args.max_age)
    print(f"  last heartbeat  {ts}  ({age:,.0f}s ago)")
    print(f"  detail          {detail}")
    if age > limit:
        sys.exit(f"\nSTALE: no heartbeat for {age:,.0f}s (limit {limit:,.0f}s). "
                 f"THE STOP-LOSS IS NOT RUNNING.")
    print(f"  OK (limit {limit:,.0f}s)")


def cmd_adjust(cfg, run, args):
    """Cash moved in or out must move the baseline with it.

    A deposit that does not raise the baseline reads as a gain and lifts the
    floor out of reach; a withdrawal that does not lower it reads as a loss and
    fires a cut that should never have happened. In an STL structure these
    transfers are yours to make, so they are recorded rather than inferred.
    """
    row = state.get(args.account)
    if not row:
        sys.exit(f"{args.account} is not enrolled.")
    delta = float(args.delta)
    dd = float(cfg["risk"]["drawdown_pct"])
    print(f"  {args.account}  baseline {row['baseline']:,.2f} "
          f"-> {row['baseline'] + delta:,.2f}   ({delta:+,.2f})")
    print(f"  floor     {row['baseline']*(1-dd):,.2f} "
          f"-> {(row['baseline']+delta)*(1-dd):,.2f}")
    guards.assert_armed(args.arm, "adjusting a baseline")
    state.adjust_baseline(args.account, delta, args.reason or "manual adjustment")
    print("  recorded.")


def cmd_reopen(cfg, run, args):
    row = state.get(args.account)
    if not row:
        sys.exit(f"{args.account} is not enrolled.")
    print(f"  {args.account} was {row['status']}, baseline {row['baseline']:,.2f}")

    # Reopening stops the police sweep, because that only runs on LOCKED
    # accounts. If the cut deferred positions to a market that had not opened
    # yet, reopening hands those positions straight back to the PM and the
    # liquidation silently never completes.
    ib, _ = _connect(cfg, run, readonly=True)
    try:
        left = cut_engine.open_positions(ib, args.account)
        orders = cut_engine.working_orders(ib, args.account)
        # "current" is the common case: the PM continues on whatever the cut
        # left them, so the baseline is simply the post-cut NLV. Reading it
        # here avoids transcribing a number by hand into a command that sets
        # someone's stop-loss.
        nlv = monitor.read_nlv(ib).get(args.account)
    finally:
        ib.disconnect()

    if str(args.baseline).lower() == "current":
        if nlv is None:
            sys.exit(f"No NLV reported for {args.account}; pass an explicit "
                     f"--baseline instead.")
        args.baseline = nlv
        print(f"  baseline = current NLV {nlv:,.2f}")

    if left or orders:
        print(f"\n  !! {len(left)} position(s) and {len(orders)} working "
              f"order(s) still open:")
        for p in left:
            print(f"       {p.contract.secType:<7}{p.contract.symbol:<10}"
                  f"{p.position:>12,.4f}")
        for t in orders:
            print(f"       ORDER  {t.contract.symbol} {t.order.action} "
                  f"{t.order.totalQuantity:,.0f}")
        print("\n  The cut has not finished. Reopening now stops the police "
              "sweep\n  and leaves these with the PM.")
        if not args.force:
            sys.exit("\nREFUSING: finish the cut first "
                     "(`cut --account %s --arm`), or pass --force if you "
                     "intend the PM to keep these." % args.account)
        print("  --force given: proceeding anyway.")

    guards.assert_armed(args.arm, "reopening a locked account")
    state.enroll(args.account, float(args.baseline), note="reopened")
    dd = float(cfg["risk"]["drawdown_pct"])
    print(f"  reopened with baseline {float(args.baseline):,.2f}, "
          f"new floor {float(args.baseline) * (1 - dd):,.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll")
    e.add_argument("--account"); e.add_argument("--all", action="store_true")
    e.add_argument("--baseline", default="current",
                   help="'current' to snapshot today's NLV, or a number")
    e.add_argument("--note", default="")

    sub.add_parser("status")

    c = sub.add_parser("cut")
    c.add_argument("--account", required=True); c.add_argument("--arm", action="store_true")

    w = sub.add_parser("watch"); w.add_argument("--arm", action="store_true")
    w.add_argument("--max-cycles", default=None,
                   help="stop after N polls (testing); default runs forever")

    h = sub.add_parser("health")
    h.add_argument("--max-age", default=120,
                   help="seconds before the heartbeat counts as stale")

    a = sub.add_parser("adjust", help="record a cash transfer in or out")
    a.add_argument("--account", required=True)
    a.add_argument("--delta", required=True,
                   help="cash moved, e.g. 50000 for a deposit, -50000 for a withdrawal")
    a.add_argument("--reason", default="")
    a.add_argument("--arm", action="store_true")

    o = sub.add_parser("reopen")
    o.add_argument("--account", required=True)
    o.add_argument("--baseline", required=True,
                   help="'current' to continue on what the cut left them, or a number")
    o.add_argument("--arm", action="store_true")
    o.add_argument("--force", action="store_true",
                   help="reopen even with positions still open (the PM keeps them)")

    args = ap.parse_args()
    run = Run(args.cmd)
    try:
        cfg = guards.load_config(CONFIG)
        if "risk" not in cfg:
            sys.exit("config.toml has no [risk] section -- copy it from "
                     "config.example.toml.")
        {"enroll": cmd_enroll, "status": cmd_status, "cut": cmd_cut,
         "watch": cmd_watch, "reopen": cmd_reopen,
         "adjust": cmd_adjust, "health": cmd_health}[args.cmd](cfg, run, args)
    except guards.GuardFailure as e:
        run.log("guard_failure", message=str(e))
        sys.exit(f"\n{e}\n")


if __name__ == "__main__":
    main()
