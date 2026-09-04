# PM 5% Drawdown Cut

Risk control for an IBKR institutional (STL) master with PM sub-accounts.

**The rule:** a PM's NLV must never fall below `0.95 × allocated capital`. On
breach the account is fully flattened and locked until manually reopened —
reopening is a re-allocation that resets the floor to 0.95 × the new number.

| Level | on $1,000,000 | Action |
|---|---|---|
| −3% | 970,000 | Warning *(not built — deferred)* |
| −4% | 960,000 | IBKR native restriction → closing orders only *(unavailable, see below)* |
| −5% | 950,000 | Flatten + lock |

## Setup

```bash
cp config.example.toml config.toml     # set port, accounts, thresholds
/opt/anaconda3/bin/python3 riskctl.py status
```

TWS or IB Gateway logged into the **master**, API enabled, **Read-Only API off**,
and **Global Configuration → API → Bypass Order Precautions for API Orders ON**
(see Gotchas). Paper ports only — anything else is refused.

## Use

```bash
riskctl.py enroll --all --baseline current     # or --baseline 100000
riskctl.py status                              # NLV vs floor, all accounts
riskctl.py cut --account DUQ782853             # dry run
riskctl.py cut --account DUQ782853 --arm       # real
riskctl.py watch                               # detect + report only
riskctl.py watch --arm                         # detect + CUT   <- production mode
riskctl.py health --max-age 120                # exit 1 = stop-loss NOT running
riskctl.py reopen --account DUQ782853 --baseline 950000 --arm
```

`watch` without `--arm` connects read-only and physically cannot trade.
Nothing that changes an account runs without `--arm`.

## Files

```
guards.py       refusals: paper ports only, explicit allowlist, --arm
state.py        SQLite baselines, status, locks, heartbeat, audit
monitor.py      NLV vs floor; warn/breach; false-trigger guards
cut_engine.py   cancel -> enumerate -> risk-order -> submit -> report
reconcile.py    before/after position and cash tie-out
riskctl.py      operator CLI
probe.py        Phase 0 capability probe (kept as evidence)
```

## How the cut works

1. Snapshot positions, cash, NLV
2. Status → CUTTING
3. **Cancel all working orders** (a PM's resting buy would refill underneath us)
4. Enumerate positions, dropping zero-quantity rows
5. Resolve each contract **by `conId` alone** — IBKR supplies the venue
6. Skip instruments whose market is shut, from contract trading hours
7. Sanity-check: quantity must equal the position exactly; direction must reduce
8. Sort by unboundedness: short options → short equities → futures → long
   options → plain longs
9. Submit market orders with `order.account = <sub>`, 0.4s apart for pacing
10. Watch all concurrently until filled or timeout
11. Snapshot after, reconcile cash against **this cut's own fills**
12. Status → LOCKED

Locked accounts are re-swept every `police_seconds` to finish positions whose
market was shut and to flatten anything the PM re-entered.

## Design notes

**The trigger reads NLV from `reqAccountSummary`**, computed server-side by
IBKR. Deliberate: there is no market data subscription here, so anything priced
locally would be wrong or absent. The rule works anyway.

**Two guards against a false cut.** A breach must persist `confirm_samples`
consecutive polls, and a one-poll drop larger than `max_jump_pct` is treated as
bad data — a stale mark can collapse NLV with no trade happening, and cutting on
that is unrecoverable.

**Unwind order is by unboundedness, not P&L.** Closing only the losers in a
derivatives book can leave a naked short leg — a risk control that increases
risk.

**Sanity checks replace the bypassed TWS precautions.** Bypassing them is
mandatory for headless running (a modal dialog hangs API orders forever with no
operator to dismiss it), so the checks live in code instead.

## LOCKED is bookkeeping, not enforcement

**`LOCKED` is a row in `risk_state.db`. IBKR knows nothing about it.** There is
no TWS API call that restricts a sub-account, so a stopped-out PM can place an
order immediately and it will be accepted. Verified by test.

The only real lock is **Layer 1** — Pre-Trade Compliance "Triggered by Loss",
configured once per sub-account in the portal, after which IBKR blocks orders
server-side with no script involved.

**It is unavailable here.** Master `DIP087996` is Customer Type **Broker
(Demo)**; the demo portal has no Trade Configuration section. Consequences:

- No protection at all while the watchdog is down
- Re-entry is caught only reactively, up to `police_seconds` later
- Must be configured and verified on a real (funded) institutional account

| | Police pass (what exists) | Layer 1 (what doesn't) |
|---|---|---|
| Nature | Reactive — undoes the trade | Preventive — refuses the order |
| Exposure window | Up to `police_seconds` | None |
| If watchdog dies | No protection | Still enforcing |

Manual fallbacks to verify on the real account: revoke a sub-account's trading
permissions, or suspend the trader's login.

## Gotchas (all cost real debugging time)

- **API orders hang silently at `PendingSubmit` with `permId=0` and no error**
  whenever TWS holds any modal dialog. Fix: Bypass Order Precautions for API
  Orders. Fatal headless — nobody there to click OK.
- **Resolve contracts by `conId` only, never pass an exchange.** Guessing SMART
  fails for every non-US equity, every future and all crypto — and a dry run
  still reports the position as closeable. Success reported while placing
  nothing is the worst failure available.
- **`reqAccountSummary` is a subscription, not a poll.** Re-requesting hits
  Error 322, after which values silently stop updating and the watchdog runs
  blind behind a green heartbeat. Appeared in three separate files. Single
  subscription point: `monitor.ensure_account_summary()`.
- **Reconcile only the cut's own fills.** `ib.trades()` and `reqExecutions`
  return the whole day, so a PM's earlier buys net against the cut's sells and a
  fully liquidated book reconciles as "nothing happened".
- **Cash is base-currency**, so a multi-currency book drifts on FX and interest
  with no trades. Tolerance scales with account size, not a flat dollar amount.
- `qualifyContracts` returns `[None]` on failure rather than raising.
- Crypto needs PAXOS, and IBKR rejects DAY market orders in crypto (must be IOC).
- Fractional quantities print as `0.00` at 2dp — a real position displayed as zero.
- Run under systemd with `python3 -u` or stdout buffering swallows the logs.

## Test status

**Proven against live paper accounts:** order placement into a sub-account,
cancelling a PM-placed order, armed cut with real fills, natural partial fills,
multi-session deferral, lock, police sweep, crash-mid-cut recovery (no
double-sell), unattended `watch --arm` trigger, reconciliation to the cent,
reopen with baseline reset, heartbeat.

**Not done:** induced rejects, NLV staleness detection, Telegram, EC2
deployment, Layer 1, and options / short-side unwind (no such positions exist in
the test book, so the two highest-priority rungs of the ladder are untested).

**Go/no-go:** no-go on the demo structure — a stop-out cannot be enforced there,
only cleaned up after. Go on a real institutional account once Layer 1 is
configured and verified.

## EC2 handoff

- IB Gateway headless + **IBC** for the daily forced restart
- `riskctl.py watch --arm` as a **systemd** service, restart-on-failure, `-u`
- API port bound to localhost, never exposed
- **Bypass Order Precautions for API Orders must be ON**
- `riskctl.py health` on a cron; exit 1 means the stop-loss is not running
- Market data subscriptions on the master before trusting execution quality
