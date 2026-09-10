# PM 7% Drawdown Cut

Risk control for an IBKR institutional (STL) master with PM sub-accounts.

**The rule:** a PM's NLV must never fall below `0.93 × allocated capital`. On
breach the account is fully flattened and locked until manually reopened —
reopening is a re-allocation that resets the floor to 0.93 × the new number.

| Level | on $1,000,000 | Action |
|---|---|---|
| −3.5% | 965,000 | Telegram warning, once per SGT day while below |
| −7% | 930,000 | Flatten + lock |

## Setup

```bash
cp config.example.toml config.toml     # set port, accounts, thresholds
/opt/anaconda3/bin/python3 riskctl.py status
```

TWS or IB Gateway logged into the **master**, API enabled, **Read-Only API off**,
and **Global Configuration → API → Bypass Order Precautions for API Orders ON**
(see Gotchas). Paper ports only — anything else is refused.

## Use

**The stop-loss itself.** This runs continuously; everything else supports it.

```bash
riskctl.py watch --arm        # poll, warn at 3.5%, liquidate + lock at 7%
riskctl.py watch              # same detection, read-only -- cannot trade
```

**Set up, and after any re-allocation.** An account that is not enrolled is not
monitored at all.

```bash
riskctl.py enroll --all --baseline current      # snapshot today's NLV
riskctl.py enroll --account DUQ782853 --baseline 100000
```

**Looking at things.** All read-only.

```bash
riskctl.py status                     # NLV vs floor, headroom, flags
riskctl.py health --max-age 120       # for cron: exit 1 = stop-loss NOT running
riskctl.py notify-test --to both      # prove Telegram before it matters
```

**Operator actions.**

```bash
riskctl.py reopen --account DUQ782853 --baseline current --arm   # after a stop-out
riskctl.py adjust --account DUQ782853 --delta 50000 --arm        # cash moved in/out
riskctl.py cut --account DUQ782853 --arm                         # manual liquidation
```

`cut` is rarely needed by hand — `watch --arm` fires it automatically. It is for
testing, and for finishing a cut that deferred positions to a closed market.

Nothing that changes an account runs without `--arm`; every such command prints
a dry run and refuses first.

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

Telegram verified live 2026-09-10 through the real trigger path: baseline
moved to −4.50% produced one warning to the group and no cut; moved to −7.50%
produced three-sample confirmation, liquidation of four US positions,
reconciliation tying to the cent, the stop-out message, and the lock.

**Not done:** induced rejects, NLV staleness detection, EC2
deployment, Layer 1, and options / short-side unwind (no such positions exist in
the test book, so the two highest-priority steps of the unwind order are
untested).

**Go/no-go:** no-go on the demo structure — a stop-out cannot be enforced there,
only cleaned up after. Go on a real institutional account once Layer 1 is
configured and verified.

## Deploying to a different master account

Everything in this repo was built and verified against a **testing** master
(`DIP087996`) with 36 paper sub-accounts. The real PM accounts sit under a
different master. Nothing here has been proven against that structure, and
several things are specific to the one it was built on.

There is no fundamental obstacle — the mechanism is `order.account = <sub>`
from a master session, which is standard for any advisor/institutional
structure. But **re-run the capability probe before arming anything.**

### Must be re-derived, not copied

| | Why |
|---|---|
| `[accounts].targets` | Different account codes entirely. Get them from `probe.py discover`. |
| **The master's own code** | Must be **excluded** from targets. If the master is enrolled it can be flattened like any other account. This is the single most dangerous misconfiguration available. |
| `risk_state.db` | Do **not** copy it over. Baselines belong to the old accounts. Start fresh and `enroll` against the real ones. |
| Baselines | Each PM's actual allocated capital on the new structure. |

### Check before arming

- **Is it a paper account?** The guards refuse any port that is not 7497 or
  4002. Pointed at a live gateway it will not run at all — deliberately.
- **Does the master have trading authority over the subs?** Proven here, not
  there. `probe.py` answers it in about five minutes.
- **Is `clientId 0` free** on that gateway? It is the only id fed
  manually-placed TWS orders.
- **Is the structure also a demo?** If the real master is a funded
  institutional account, Pre-Trade Compliance may be available — which would
  give a genuine preventive lock instead of the reactive police sweep. Worth
  checking first; it changes the risk profile substantially.
- **Base currency.** NLV is reported in the master's base currency. Baselines
  must be set in the same currency; the code compares numbers, not units.
- **Trading permissions.** The master must be permitted to trade every
  instrument the PMs hold, or closing orders will reject.

## EC2 deployment runbook

**1. Prove the capability first.**

```bash
cp config.example.toml config.toml     # port, and the real sub-accounts
python probe.py discover               # read-only: accounts, NLV, positions, orders
```

Confirm the sub-accounts appear, and that the master's own code is **not** in
`targets`. Then confirm the master can act inside a sub-account: from a
sub-account login leave a resting order that cannot fill, then

```bash
python probe.py cancel --account <SUB> --perm-id <id> --arm
```

If that cancel fails, stop — the cut is unsafe, because a PM's resting order
will refill positions while the engine is selling them.

**2. Instance.** t3.medium or larger; IB Gateway plus the JVM needs the
headroom.

**3. IB Gateway headless, with IBC.** IBKR force-restarts the gateway daily.
Without IBC it dies and the stop-loss dies with it.

**4. Gateway configuration.** All three matter:

- API enabled
- **Read-Only API OFF** — the engine must be able to place orders
- **Bypass Order Precautions for API Orders ON** — without it TWS raises a
  modal dialog and every API order hangs at `PendingSubmit` forever with no
  error. There is no operator on a headless box to dismiss it, so the
  stop-loss fails silently while appearing healthy.
- API port bound to **localhost only**. It has no authentication beyond IP
  allowlisting; never expose 4001/4002.

**5. Secrets.** `config.toml` is gitignored and therefore not in the clone. It
holds the bot token and both Telegram chat IDs. Without them the watchdog runs
perfectly and sends nothing — silently, because delivery failure is
deliberately non-fatal. Get them out-of-band and create the file on the box.

**6. Enroll.**

```bash
python riskctl.py enroll --all --baseline 100000    # or --baseline current
python riskctl.py status                            # verify every floor
```

**7. Prove alerting from that machine**, not just from someone's laptop:

```bash
python riskctl.py notify-test --to both
```

**8. Run it as a service.**

```ini
[Unit]
Description=PM drawdown stop-loss
After=network-online.target

[Service]
WorkingDirectory=/opt/pm-risk-cut
ExecStart=/usr/bin/python3 -u riskctl.py watch --arm
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`-u` is not optional: without it stdout buffering swallows the logs.

**9. Deadman on a cron.** Exit code 1 means the stop-loss is not running.

```
*/5 * * * * cd /opt/pm-risk-cut && /usr/bin/python3 riskctl.py health --max-age 120
```

`health` sends the alert itself; wire the exit code to your own monitoring too.

**10. Verify end to end before trusting it.** Move one account's baseline so it
sits below the floor, watch the cut fire and the Telegram message arrive, then
reopen. Every bug found in this project appeared the first time something ran
armed rather than dry.

### Known gaps at handover

- **Layer 1 is not configured.** Locking is bookkeeping only; a stopped-out PM
  can still trade, and the police sweep flattens them reactively up to
  `police_seconds` later. Configure Pre-Trade Compliance if the real structure
  supports it.
- **No market data subscription** on the test master. NLV is unaffected
  (server-computed), but the engine sends market orders blind. Fine for liquid
  US names, risky for illiquid non-US ones.
- **Options and short positions are untested** — none existed in the test book,
  so the two highest-priority steps of the unwind order have never executed.

## Telegram alerts

Three events, two destinations:

| Event | Goes to |
|---|---|
| −3.5% warning | PM group |
| 7% stop-out, with what closed / deferred / failed | PM group |
| Watchdog dead | your private chat, **not** the group |

The warning fires **once per Singapore calendar day and repeats daily** while
the account stays below the line — the point is to prompt action, not to file a
single alert. It re-arms on reopen.

Configure `bot_token`, `group_chat_id` and `alert_chat_id` under `[telegram]`
in `config.toml` (gitignored — the token never reaches the repo), then
`riskctl.py notify-test --to both`.

Optional `[labels]` maps account codes to PM names, so messages read
`DUQ782853 (Wei Ming)` instead of a bare code.

Delivery failure can never take down the watchdog: every send is wrapped, logs
to stderr, and returns rather than raising. A Telegram outage is inconvenient;
it is not a reason to stop enforcing a stop-loss.

## Reopening a locked account

Locking is deliberate — reopening is a decision you make, not a timer.

**1. Check the cut actually finished.** `reopen` refuses if positions or working
orders remain, because reopening stops the police sweep: anything the cut
deferred to a closed market would silently become the PM's again. Finish it with
`cut --account X --arm`, or pass `--force` if you intend them to keep it.

**2. Decide the new allocation.** This is the real decision. Three shapes:

| | Do this |
|---|---|
| Continue on what's left | Reopen at their post-cut NLV |
| Reduced size | Transfer cash **out** to master first, then reopen at the lower figure |
| Topped back up | Transfer cash **in** first, then reopen at the higher figure |

**3. Make the number match the cash.** The baseline must equal the capital
actually in the account. Reopening at 500k while the account holds 950k makes
the floor fiction.

**4. Reopen.** For the usual case — the PM continues on whatever the cut left
them — `current` reads the post-cut NLV so no figure is transcribed by hand:

```bash
riskctl.py reopen --account DUQ782853 --baseline current --arm   # continue on what's left
riskctl.py reopen --account DUQ782853 --baseline 500000 --arm    # re-allocated figure
```

Run it wherever the code and a Gateway connection live — your Mac now, the EC2
box later. `reopen` connects read-only to check for leftover positions.

The floor recomputes to 0.93 × the new baseline — so a PM stopped out at 930k
and reopened there has a new floor of 864,900, not their old one.

**5. Show them the record.** Every enroll, status change and adjustment is in the
`audit` table of `risk_state.db`, timestamped.

### Cash transfers while a PM is active

```bash
riskctl.py adjust --account DUQ782853 --delta 50000 --reason "Q3 top-up" --arm
```

A deposit that does not raise the baseline reads as a gain and lifts the floor
out of reach; a withdrawal that does not lower it reads as a loss and fires a
cut that should never have happened. Record every transfer.
