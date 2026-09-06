"""Telegram delivery.

One rule: notification failure must never take down the watchdog. A Telegram
outage, a revoked token, a group the bot was removed from -- all of those are
inconvenient, none of them are a reason to stop enforcing a stop-loss. Every
send is wrapped, logs to stderr, and returns False rather than raising.

Uses urllib from the stdlib deliberately, so deployment needs no extra package.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TIMEOUT_S = 10
SGT = ZoneInfo("Asia/Singapore")


def today_sgt() -> str:
    """Singapore calendar date -- the day boundary for 'one warning per day'.

    The book spans six market sessions, but the people reading these messages
    are in Singapore, so a 'day' is their day.
    """
    return datetime.now(SGT).date().isoformat()


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def label_for(cfg, account: str) -> str:
    """'DUQ782853 (Alice)' if a label is configured, else the bare code."""
    name = (cfg.get("labels") or {}).get(account)
    return f"{account} ({name})" if name else account


def configured(cfg) -> bool:
    tg = cfg.get("telegram") or {}
    return bool(tg.get("bot_token"))


def send(cfg, text: str, *, to: str = "group") -> bool:
    """to='group' for the PM chat, to='alert' for the private one."""
    tg = cfg.get("telegram") or {}
    token = tg.get("bot_token")
    chat = tg.get("group_chat_id") if to == "group" else tg.get("alert_chat_id")
    if not token or not chat:
        print(f"  [notify] no {to} chat configured -- message not sent",
              file=sys.stderr)
        return False

    payload = urllib.parse.urlencode({
        "chat_id": str(chat), "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(url, payload, timeout=TIMEOUT_S) as r:
            body = json.load(r)
        if not body.get("ok"):
            print(f"  [notify] telegram refused: {body.get('description')}",
                  file=sys.stderr)
            return False
        return True
    except Exception as e:                       # never propagate
        print(f"  [notify] send failed: {e}", file=sys.stderr)
        return False


# --------------------------------------------------------------- messages --

def warn_text(cfg, r: dict, drawdown_pct: float) -> str:
    return (
        f"⚠️ <b>Drawdown warning — {_esc(label_for(cfg, r['account']))}</b>\n\n"
        f"Down {abs(r['drawdown']):.1%} from allocated capital.\n\n"
        f"<pre>"
        f"Current NLV   {r['nlv']:>12,.0f}\n"
        f"Stop-out at   {r['floor']:>12,.0f}   (−{drawdown_pct:.0%})\n"
        f"Room left     {r['headroom']:>12,.0f}"
        f"</pre>\n\n"
        f"Positions are liquidated automatically if NLV reaches "
        f"the stop-out level."
    )


def stop_text(cfg, account: str, report: dict, nlv: float, baseline: float,
              drawdown: float) -> str:
    lines = [f"🛑 <b>STOP-OUT — {_esc(label_for(cfg, account))}</b>\n",
             f"Down {abs(drawdown):.1%}. Book liquidated, account locked.\n",
             f"<pre>NLV at cut  {nlv:>12,.0f}\nBaseline    {baseline:>12,.0f}</pre>"]

    def block(title, rows, fmt):
        if not rows:
            return
        lines.append(f"\n<b>{title}</b>\n<pre>"
                     + "\n".join(fmt(x) for x in rows) + "</pre>")

    block("Closed", report.get("closed"),
          lambda x: f"{_esc(x['instrument']):<22} {_esc(x.get('status',''))}")
    block("Deferred — market closed", report.get("deferred"),
          lambda x: f"{_esc(x['instrument']):<22} {_esc(x.get('reason',''))}")
    block("FAILED — needs manual action", report.get("failed"),
          lambda x: f"{_esc(x['instrument']):<22} {_esc(x.get('reason',''))}")
    block("Unfilled", report.get("residual"),
          lambda x: f"{_esc(x['instrument']):<22} {_esc(x.get('status',''))}")

    if report.get("deferred"):
        lines.append("\nDeferred positions close when their market opens.")
    lines.append("\nAccount stays locked until reviewed.")
    return "\n".join(lines)


def deadman_text(age_s: float, last_seen: str) -> str:
    try:
        seen = datetime.fromisoformat(last_seen).astimezone(SGT).strftime(
            "%Y-%m-%d %H:%M SGT")
    except (TypeError, ValueError):
        seen = last_seen
    return (f"🔴 <b>Risk watchdog is not running</b>\n\n"
            f"No heartbeat for {age_s/60:.0f} minutes.\n"
            f"The stop-loss is <b>NOT active</b> on any account.\n\n"
            f"<pre>Last seen  {_esc(seen)}</pre>")
