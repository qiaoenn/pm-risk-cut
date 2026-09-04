"""The watchdog: read NLV, compare to each account's floor, fire the cut.

The trigger deliberately uses NetLiquidation from reqAccountSummary, which
IBKR computes server-side from its own prices. That matters here: this setup
has no market data subscription, so anything we priced ourselves would be
wrong or absent. NLV is unaffected by that gap.

Two guards against a false cut, which is the failure mode that costs a PM
their book for no reason:

  · a breach must persist across `confirm_samples` consecutive polls, and
  · an implausible one-poll drop is treated as suspect data, not as a loss.
"""

import state

WARN, BREACH, OK = "WARN", "BREACH", "OK"


# reqAccountSummary is a SUBSCRIPTION, not a poll. Re-requesting it every
# cycle exhausts IBKR's limit ("Error 322: Maximum number of account summary
# requests exceeded"), after which values stop updating -- and the watchdog
# goes blind while still looking alive. Subscribe once per connection; the
# values are pushed thereafter.
_SUBSCRIBED: set[int] = set()


def ensure_account_summary(ib, settle_s: float = 2.5) -> None:
    """Subscribe once per connection. EVERY caller must come through here.

    This bit me twice. The first time the watch loop re-subscribed each poll;
    the second time reconcile.snapshot() called reqAccountSummary directly and
    re-introduced the same fault from a different file. Once IBKR refuses, the
    cached values stop updating and the watchdog runs blind with a perfectly
    healthy heartbeat -- so there is exactly one subscription point now.
    """
    if id(ib) not in _SUBSCRIBED:
        ib.reqAccountSummary()
        _SUBSCRIBED.add(id(ib))
        ib.sleep(settle_s)
    else:
        ib.sleep(0.2)          # yield to the event loop; values arrive pushed


def read_nlv(ib, settle_s: float = 2.5) -> dict:
    ensure_account_summary(ib, settle_s)
    out = {}
    for av in ib.accountSummary():
        if av.tag == "NetLiquidation":
            try:
                out[av.account] = float(av.value)
            except (TypeError, ValueError):
                continue
    return out


def assess(row, nlv: float, drawdown_pct: float, warn_pct: float) -> dict:
    baseline = float(row["baseline"])
    floor = state.floor_for(row, drawdown_pct)
    dd = (nlv / baseline) - 1.0 if baseline else 0.0
    level = BREACH if nlv <= floor else (WARN if dd <= -warn_pct else OK)
    return {"account": row["account"], "nlv": nlv, "baseline": baseline,
            "floor": floor, "drawdown": dd, "level": level,
            "status": row["status"],
            "headroom": nlv - floor}


def evaluate(ib, cfg) -> list:
    risk = cfg.get("risk", {})
    dd = float(risk.get("drawdown_pct", 0.05))
    warn = float(risk.get("warn_pct", 0.03))
    nlv = read_nlv(ib)
    return [assess(r, nlv[r["account"]], dd, warn)
            for r in state.all_accounts() if r["account"] in nlv]


def confirm(history: dict, account: str, level: str, needed: int) -> bool:
    """Count consecutive breaching samples; any non-breach resets the count."""
    if level != BREACH:
        history[account] = 0
        return False
    history[account] = history.get(account, 0) + 1
    return history[account] >= needed


def implausible(prev: float | None, now: float, max_jump_pct: float) -> bool:
    """A drop too large to be real between two polls seconds apart.

    A stale or broken mark can make NLV collapse without a single trade
    happening. Firing a cut on that is unrecoverable, so a jump this large is
    treated as bad data: it is logged, and it does not count toward
    confirmation.
    """
    if prev is None or prev <= 0:
        return False
    return (now / prev - 1.0) <= -abs(max_jump_pct)


def render(rows: list) -> str:
    out = [f"  {'account':<12} {'status':<8} {'NLV':>14} {'floor':>14} "
           f"{'drawdown':>9} {'headroom':>13}  flag"]
    for r in sorted(rows, key=lambda x: x["drawdown"]):
        flag = {"BREACH": "<<< BREACH", "WARN": "<-- warn"}.get(r["level"], "")
        out.append(f"  {r['account']:<12} {r['status']:<8} {r['nlv']:>14,.2f} "
                   f"{r['floor']:>14,.2f} {r['drawdown']:>8.2%} "
                   f"{r['headroom']:>13,.2f}  {flag}")
    return "\n".join(out)
