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


def _fmt_dow_date(date_str: str) -> str:
    """Day-of-week + date, e.g. 'Thu 8 Oct'. Sophie's commute switched from Tue to Thu
    on 1 Oct 2026, so the bullet list needs to show whichever day each entry actually is
    rather than a hardcoded 'Tue' prefix."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return d.strftime("%a %-d %b")


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


CHANGE_THRESHOLD_GBP = 3.00  # ignore movement smaller than this — likely SplitSave noise


def _is_new_low(t: dict) -> bool:
    """True if today's cheapest is strictly below every prior observation in history."""
    cur = t.get("current") or {}
    today_total = cur.get("cheapest_any_total")
    if today_total is None:
        return False
    history = t.get("history") or []
    prior_totals = [
        h.get("cheapest_any_total") for h in history
        if h.get("cheapest_any_total") is not None
    ]
    if not prior_totals:
        return False  # first observation — can't claim "new" low
    return today_total < min(prior_totals)


def _movements(tuesdays: list) -> dict:
    """Bucket today's tracked dates by what actually changed since yesterday.

    Lead with movement, not status — Sophie's complaint is the daily 'Book X today'
    nag for dates that haven't moved in weeks. A flat £70.80 isn't news on day 7,
    even if it's below the £85 sweet-spot threshold."""
    drops, rises, new_lows = [], [], []
    for t in tuesdays:
        change = (t.get("change_vs_yesterday") or {}).get("cheapest_any")
        if change is not None:
            if change <= -CHANGE_THRESHOLD_GBP:
                drops.append(t)
            elif change >= CHANGE_THRESHOLD_GBP:
                rises.append(t)
        if _is_new_low(t):
            new_lows.append(t)
    return {"drops": drops, "rises": rises, "new_lows": new_lows}


def compose_headline(tuesdays: list) -> str:
    """One-line lead based on what changed since yesterday.

    Priority: drops > new historical lows > rises > nothing-to-report. Status
    flags (BOOK_TODAY etc.) deliberately do not drive the headline anymore —
    they fired every day for static-cheap dates and trained Sophie to ignore."""
    m = _movements(tuesdays)

    if m["drops"]:
        # Biggest absolute drop wins — that's the most actionable price news.
        t = max(m["drops"], key=lambda x: abs(x["change_vs_yesterday"]["cheapest_any"]))
        change = t["change_vs_yesterday"]["cheapest_any"]
        price, source = _best_price_and_source(t["current"])
        return f"Price drop: {_fmt_dow_date(t['date'])} down {_fmt_gbp(abs(change))} to {_fmt_gbp(price)} ({source})."
    if m["new_lows"]:
        t = sorted(m["new_lows"], key=lambda x: x["date"])[0]
        price, source = _best_price_and_source(t["current"])
        return f"New low: {_fmt_dow_date(t['date'])} just hit {_fmt_gbp(price)} ({source}). Cheapest seen so far."
    if m["rises"]:
        t = max(m["rises"], key=lambda x: x["change_vs_yesterday"]["cheapest_any"])
        change = t["change_vs_yesterday"]["cheapest_any"]
        price, source = _best_price_and_source(t["current"])
        return f"Heads up: {_fmt_dow_date(t['date'])} up {_fmt_gbp(change)} to {_fmt_gbp(price)} ({source})."
    return "No changes today."


def compose_body(tuesdays: list, ranked: list, max_lines: int = 3) -> str:
    """Body lines after the headline.

    On a movement day, list the dates that actually moved.
    On a quiet day, show only the cheapest unbooked date as a one-line reference
    — no daily list of every tracked Tuesday/Thursday, that's exactly the noise
    Sophie asked us to remove."""
    m = _movements(tuesdays)
    moved = m["drops"] + m["new_lows"] + m["rises"]

    if moved:
        # Show movers first, dedupe, cap at max_lines.
        seen, out = set(), []
        for t in moved:
            if t["date"] in seen:
                continue
            seen.add(t["date"])
            cur = t.get("current") or {}
            price, source = _best_price_and_source(cur)
            arrow = _change_arrow(t.get("change_vs_yesterday"))
            out.append(f"• {_fmt_dow_date(t['date'])}: {_fmt_gbp(price)} ({source}){arrow}")
            if len(out) >= max_lines:
                break
        return "\n".join(out)

    # Quiet day — one-line reference to the cheapest unbooked date, no list.
    cheapest = min(
        (t for t in tuesdays if (t.get("current") or {}).get("cheapest_any_total") is not None),
        key=lambda x: x["current"]["cheapest_any_total"],
        default=None,
    )
    if cheapest is None:
        return ""
    price, source = _best_price_and_source(cheapest["current"])
    return f"Cheapest unbooked: {_fmt_dow_date(cheapest['date'])} at {_fmt_gbp(price)} ({source})."


def _source_caveat(tuesdays: list) -> str:
    """If every snapshot used the NR fallback, warn Sophie so she taps through for SplitSave."""
    sources = {((t.get("current") or {}).get("source") or "") for t in tuesdays}
    if sources == {"national_rail"}:
        return ("Note: couldn't reach Trainline — these are National Rail fares. "
                "Tap through the Trainline app for SplitSave prices (often lower).")
    return ""


def compose_message(data: dict) -> str:
    today = date.today().strftime("%a %-d %b")
    # Exclude already-booked Tuesdays — Sophie doesn't want nagging on dates she's paid for.
    tuesdays = [
        t for t in data.get("tuesdays", [])
        if t.get("current") and not t.get("booked")
    ]
    if not tuesdays:
        return (f"Morning Soph — nothing to action today. All tracked Tuesdays are either "
                f"booked or awaiting data. Full tracker: {TRACKER_URL}")
    ranked = rank_tuesdays(tuesdays)
    headline = compose_headline(tuesdays)
    body = compose_body(tuesdays, ranked, max_lines=3)
    caveat = _source_caveat(tuesdays)
    parts = [f"Morning Soph ({today})", headline]
    if body:
        parts.append(body)
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
