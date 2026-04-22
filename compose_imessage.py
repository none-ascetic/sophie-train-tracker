#!/usr/bin/env python3
"""Compose Sophie's daily iMessage from prices.json.

Called at the end of the 02:00 scheduled task — after all snapshots are in. Writes
to pending_message.txt; the 06:00 sender task reads that file and fires the actual
iMessage via the iMessage MCP.

Tone rules (matters — Sophie is the end user, not Paddy):
  - British English, sisterly, no corporate polish
  - Lead with a verb, not a price ("Book today", "Hold off", "Nothing to do")
  - Max 4-5 short lines + one tracker link
  - When Trainline data is missing (bot detection, Chrome offline), say so
  - Flag price drops with ↓ arrows, rises with ↑
"""
import json
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).parent
PRICES = ROOT / "prices.json"
PENDING = ROOT / "pending_message.txt"
TRACKER_URL = "https://sophie-train-tracker.vercel.app"

# The thresholds match update_prices.py — kept here too so messaging logic is self-contained.
SWEET_SPOT = 85.00
ACCEPTABLE = 110.00
BASELINE = 127.00


def _fmt_gbp(x):
    if x is None:
        return "—"
    return f"£{x:.0f}" if x == int(x) else f"£{x:.2f}"


def _fmt_date(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return d.strftime("%-d %b")


def _best_price_and_source(current: dict) -> tuple:
    """Returns (price, source_label) — picks splitsave if lower than direct."""
    direct = current.get("cheapest_any_total")
    splitsave = (current.get("splitsave") or {}).get("total")
    if splitsave is not None and (direct is None or splitsave < direct):
        return splitsave, "SplitSave"
    return direct, "direct"


def _change_arrow(change: dict) -> str:
    """↓ £8 / ↑ £5 / '' (no change)"""
    if not change:
        return ""
    # Prefer the best-price direction (any vs splitsave) — use cheapest_any as proxy
    delta = change.get("cheapest_any") or change.get("splitsave")
    if delta is None or delta == 0:
        return ""
    if delta < 0:
        return f" (↓ {_fmt_gbp(abs(delta))} vs yesterday)"
    return f" (↑ {_fmt_gbp(delta)} vs yesterday)"


def rank_tuesdays(tuesdays: list) -> list:
    """Sort Tuesdays by urgency (what Sophie should act on first).

    BOOK_TODAY first (best deal, act now), then URGENT (cheap tier gone — limit
    damage), then BOOK_SOON, then STABLE (watching only). Ties break by date
    ascending — the soonest travel date wins within a status bucket."""
    def key(t):
        status_order = {"BOOK_TODAY": 0, "URGENT": 1, "BOOK_SOON": 2, "STABLE": 3, "UNKNOWN": 4}
        return (status_order.get(t.get("status"), 5), t.get("date", ""))
    return sorted(tuesdays, key=key)


def compose_headline(tuesdays: list) -> str:
    """One-line lead based on the best available action today."""
    book_today = [t for t in tuesdays if t.get("status") == "BOOK_TODAY"]
    urgent = [t for t in tuesdays if t.get("status") == "URGENT"]
    drops = [
        t for t in tuesdays
        if (t.get("change_vs_yesterday") or {}).get("cheapest_any") is not None
        and t["change_vs_yesterday"]["cheapest_any"] < -5  # dropped > £5
    ]

    if book_today:
        t = book_today[0]
        price, source = _best_price_and_source(t["current"])
        return f"Book {_fmt_date(t['date'])} today — {_fmt_gbp(price)} return ({source}). Cheapest I've seen."
    if drops:
        t = drops[0]
        change = t["change_vs_yesterday"]["cheapest_any"]
        price, source = _best_price_and_source(t["current"])
        return f"Price drop: {_fmt_date(t['date'])} down {_fmt_gbp(abs(change))} to {_fmt_gbp(price)} ({source})."
    if urgent:
        # Pick the soonest URGENT (earliest travel date) — that's what needs
        # booking right now before peak fares climb further.
        t = sorted(urgent, key=lambda x: x["date"])[0]
        price, _ = _best_price_and_source(t["current"])
        return f"Heads up: {_fmt_date(t['date'])} cheap tier gone — now {_fmt_gbp(price)}. Book today before it climbs."
    return "Nothing urgent today — cheapest fares are holding. I'll check again tomorrow."


def compose_body(ranked: list, max_lines: int = 3) -> str:
    """A few short context lines after the headline."""
    lines = []
    for t in ranked[:max_lines]:
        cur = t.get("current") or {}
        price, source = _best_price_and_source(cur)
        arrow = _change_arrow(t.get("change_vs_yesterday"))
        lines.append(f"• Tue {_fmt_date(t['date'])}: {_fmt_gbp(price)} ({source}){arrow}")
    return "\n".join(lines)


def _source_caveat(tuesdays: list) -> str:
    """If every snapshot used the NR fallback, warn Sophie so she taps through for SplitSave."""
    sources = {((t.get("current") or {}).get("source") or "") for t in tuesdays}
    if sources == {"national_rail"}:
        return ("Note: couldn't reach Trainline — these are National Rail fares. "
                "Tap through the Trainline app for SplitSave prices (often lower).")
    return ""


def compose_message(data: dict) -> str:
    today = date.today().strftime("%a %-d %b")
    tuesdays = [t for t in data.get("tuesdays", []) if t.get("current")]
    if not tuesdays:
        return (f"Morning Soph — couldn't reach Trainline last night, so no update today. "
                f"Will try again tomorrow. Full tracker: {TRACKER_URL}")
    ranked = rank_tuesdays(tuesdays)
    headline = compose_headline(tuesdays)
    body = compose_body(ranked, max_lines=3)
    caveat = _source_caveat(tuesdays)
    parts = [f"Morning Soph ({today})", headline, body]
    if caveat:
        parts.append(caveat)
    parts.append(f"Full tracker: {TRACKER_URL}")
    return "\n\n".join(parts)


def main():
    data = json.loads(PRICES.read_text())
    msg = compose_message(data)
    PENDING.write_text(msg + "\n")
    print(f"Wrote {len(msg)} chars to {PENDING}")


if __name__ == "__main__":
    main()
