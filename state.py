"""Durable risk state. SQLite, because this must survive a process restart.

The two facts that cannot live in memory:

  · the baseline each account's floor is computed from, and
  · whether an account is already locked.

If either is lost on restart, the watchdog either re-cuts an account it already
cut, or silently unlocks a PM who was stopped out. Both are worse than the
watchdog simply being down.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).parent / "risk_state.db"

ACTIVE, WARNED, CUTTING, LOCKED = "ACTIVE", "WARNED", "CUTTING", "LOCKED"


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS accounts (
        account   TEXT PRIMARY KEY,
        baseline  REAL NOT NULL,
        status    TEXT NOT NULL DEFAULT 'ACTIVE',
        enrolled  TEXT NOT NULL,
        locked_at TEXT,
        note      TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS heartbeat (
        id      INTEGER PRIMARY KEY CHECK (id = 1),
        ts      TEXT NOT NULL,
        detail  TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit (
        ts      TEXT NOT NULL,
        account TEXT NOT NULL,
        event   TEXT NOT NULL,
        detail  TEXT)""")
    return c


def _now():
    return datetime.now(timezone.utc).isoformat()


def audit(account: str, event: str, detail: str = "") -> None:
    with _conn() as c:
        c.execute("INSERT INTO audit VALUES (?,?,?,?)",
                  (_now(), account, event, detail))


def enroll(account: str, baseline: float, note: str = "") -> None:
    """Set (or reset) an account's baseline. Reopening after a cut is just an
    enroll with the new allocation -- the floor recomputes from it, which is
    what stops a stopped-out PM inheriting their old, now-unreachable floor."""
    with _conn() as c:
        c.execute("""INSERT INTO accounts (account, baseline, status, enrolled, note)
                     VALUES (?,?,?,?,?)
                     ON CONFLICT(account) DO UPDATE SET
                       baseline=excluded.baseline, status='ACTIVE',
                       enrolled=excluded.enrolled, locked_at=NULL,
                       note=excluded.note""",
                  (account, float(baseline), ACTIVE, _now(), note))
    audit(account, "ENROLL", f"baseline={baseline:,.2f} {note}")


def adjust_baseline(account: str, delta: float, reason: str) -> float:
    """Cash moved in or out must move the baseline by the same amount.

    Without this a deposit reads as a gain and the floor drifts upward out of
    reach; a withdrawal reads as a loss and fires a cut that never should have
    happened. In an STL structure you control these transfers, so they are
    recorded here rather than inferred.
    """
    with _conn() as c:
        row = c.execute("SELECT baseline FROM accounts WHERE account=?",
                        (account,)).fetchone()
        if not row:
            raise KeyError(f"{account} is not enrolled")
        new = float(row["baseline"]) + float(delta)
        c.execute("UPDATE accounts SET baseline=? WHERE account=?", (new, account))
    audit(account, "BASELINE_ADJUST", f"{delta:+,.2f} -> {new:,.2f} ({reason})")
    return new


def set_status(account: str, status: str, detail: str = "") -> None:
    with _conn() as c:
        c.execute("UPDATE accounts SET status=?, locked_at=? WHERE account=?",
                  (status, _now() if status == LOCKED else None, account))
    audit(account, f"STATUS_{status}", detail)


def get(account: str):
    with _conn() as c:
        return c.execute("SELECT * FROM accounts WHERE account=?",
                         (account,)).fetchone()


def all_accounts():
    with _conn() as c:
        return c.execute("SELECT * FROM accounts ORDER BY account").fetchall()


def floor_for(row, drawdown_pct: float) -> float:
    return float(row["baseline"]) * (1.0 - drawdown_pct)


# --- liveness ---------------------------------------------------------------
# A watchdog that dies silently is worse than no watchdog, because you believe
# you are covered. `watch` stamps this every poll; `riskctl.py health` reads it
# and exits non-zero when it goes stale, so cron/systemd/an alerting hook can
# notice that the stop-loss is no longer running.

def beat(detail: str = "") -> None:
    with _conn() as c:
        c.execute("INSERT INTO heartbeat (id, ts, detail) VALUES (1,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, "
                  "detail=excluded.detail", (_now(), detail))


def last_beat():
    with _conn() as c:
        row = c.execute("SELECT ts, detail FROM heartbeat WHERE id=1").fetchone()
    return (row["ts"], row["detail"]) if row else (None, None)
