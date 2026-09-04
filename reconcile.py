"""Before/after tie-out for a cut.

A cut that "worked" is not one where the orders came back Filled -- it is one
where the positions are actually gone and the cash moved by the amount the
fills say it should have. Those are different claims, and only the second one
catches a partial fill that was reported as complete.
"""


def snapshot(ib, account: str, settle_s: float = 2.5) -> dict:
    import monitor

    ib.reqPositions()
    # NOT reqAccountSummary() -- that is a subscription, and re-requesting it
    # per cut exhausts IBKR's limit (Error 322), after which values silently
    # stop updating. monitor owns the single subscription.
    monitor.ensure_account_summary(ib, settle_s)
    ib.sleep(settle_s)
    positions = {p.contract.conId: {"symbol": p.contract.symbol,
                                    "secType": p.contract.secType,
                                    "qty": p.position}
                 for p in ib.positions()
                 if p.account == account and abs(p.position) > 1e-9}
    vals = {av.tag: av.value for av in ib.accountSummary()
            if av.account == account}
    def num(tag):
        try:
            return float(vals.get(tag, "nan"))
        except (TypeError, ValueError):
            return float("nan")
    return {"account": account, "positions": positions,
            "cash": num("TotalCashValue"), "nlv": num("NetLiquidation")}


def compare(before: dict, after: dict, fills: list, tolerance: float = 1.0,
            tolerance_pct: float = 0.0001) -> dict:
    """fills: [{shares, price, side, commission}] as reported by IBKR.

    A flat absolute tolerance does not survive a multi-currency book. IBKR
    reports TotalCashValue in the base currency, so cash drifts continuously as
    FX moves and interest accrues -- with no trade happening at all. On a $1M
    account that drift is easily tens of dollars, which a $1 tolerance reports
    as a reconciliation failure. The tolerance therefore scales with account
    size, and a run with no fills says so explicitly instead of crying
    MISMATCH at ordinary revaluation.
    """
    proceeds = 0.0
    commission = 0.0
    for f in fills:
        sign = 1.0 if str(f.get("side", "")).upper().startswith(("SLD", "SELL")) else -1.0
        proceeds += sign * float(f.get("shares", 0)) * float(f.get("price", 0))
        commission += float(f.get("commission") or 0.0)

    expected = proceeds - commission
    actual = after["cash"] - before["cash"]
    nlv = before["nlv"] if before["nlv"] == before["nlv"] else 0.0
    effective = max(tolerance, abs(nlv) * tolerance_pct)
    # NaN-safe: a missing cash figure is an unknown, not a pass.
    ties = (abs(actual - expected) <= effective
            if actual == actual and expected == expected else False)
    note = ("no fills -- any cash delta is FX revaluation or interest, not a trade"
            if not fills else "")

    remaining = {cid: v for cid, v in after["positions"].items()}
    closed = [v["symbol"] for cid, v in before["positions"].items()
              if cid not in after["positions"]]
    return {"account": before["account"],
            "positions_before": len(before["positions"]),
            "positions_after": len(after["positions"]),
            "closed": closed,
            "still_open": [f"{v['secType']} {v['symbol']} {v['qty']}"
                           for v in remaining.values()],
            "expected_cash_delta": expected, "actual_cash_delta": actual,
            "commission": commission, "ties": ties, "tolerance": effective,
            "note": note,
            "nlv_before": before["nlv"], "nlv_after": after["nlv"]}


def render(r: dict) -> str:
    out = [f"  positions   {r['positions_before']} -> {r['positions_after']}",
           f"  closed      {', '.join(r['closed']) or '(none)'}"]
    if r["still_open"]:
        out.append(f"  STILL OPEN  {'; '.join(r['still_open'])}")
    out += [f"  cash delta  expected {r['expected_cash_delta']:>14,.2f}   "
            f"actual {r['actual_cash_delta']:>14,.2f}   "
            f"commission {r['commission']:,.2f}",
            f"  NLV         {r['nlv_before']:,.2f} -> {r['nlv_after']:,.2f}",
            f"  TIE-OUT     {'OK' if r['ties'] else 'MISMATCH'} "
            f"(tolerance {r['tolerance']:,.2f})"]
    if r.get("note"):
        out.append(f"  note        {r['note']}")
    return "\n".join(out)
