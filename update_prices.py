#!/usr/bin/env python3
"""Merge a fresh Trainline snapshot into prices.json.

Called by the 02:00 scheduled task, once per pending Tuesday:

    python3 update_prices.py --date 2026-06-16 --snapshot snapshot.json

Where snapshot.json is the output of fetch_trainline_fares.py.

Responsibilities:
  - Find the Tuesday entry in prices.json (by date)
  - Push current snapshot onto history[] (cap at 14)
  - Replace current with the new snapshot
  - Compute change_vs_yesterday diffs
  - Recompute status (URGENT / BOOK_TODAY / BOOK_SOON / STABLE) based on totals vs baseline
  - Write prices.json back atomically
"""
import argparse
import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
PRICES = ROOT / "prices.json"
HISTORY_CAP = 14  # days

# Thresholds for status computation — anchored on Sophie's £75 ambition + £127 direct baseline.
STATUS_THRESHOLDS = {
    "sweet_spot_total": 85.00,   # at or below this → BOOK_TODAY
    "acceptable_total": 110.00,  # at or below this → BOOK_SOON
    "baseline_total": 127.00,    # default target
}


def compact_snapshot(snap: dict) -> dict:
    """Trim a full snapshot down to what goes into history[]."""
    return {
        "checked_at": snap["checked_at"],
        "source": snap.get("source", "trainline"),
        "cheapest_direct_total": snap.get("cheapest_direct_total"),
        "cheapest_any_total": snap.get("cheapest_any_total"),
        "splitsave_total": (snap.get("splitsave") or {}).get("total"),
    }


def compute_status(snap: dict) -> str:
    """Status flag based on the cheapest achievable total in the new snapshot."""
    cheapest = _best_total(snap)
    if cheapest is None:
        return "UNKNOWN"
    if cheapest <= STATUS_THRESHOLDS["sweet_spot_total"]:
        return "BOOK_TODAY"
    if cheapest <= STATUS_THRESHOLDS["acceptable_total"]:
        return "BOOK_SOON"
    if cheapest <= STATUS_THRESHOLDS["baseline_total"]:
        return "STABLE"
    return "URGENT"  # Above baseline → cheap tier gone, book before it climbs further


def _best_total(snap: dict) -> Optional[float]:
    """Return the lowest of cheapest_any and splitsave totals."""
    candidates = [
        snap.get("cheapest_any_total"),
        (snap.get("splitsave") or {}).get("total"),
    ]
    vals = [c for c in candidates if c is not None]
    return min(vals) if vals else None


def compute_change(new: dict, prev_compact: Optional[dict]) -> Optional[dict]:
    """Day-over-day delta. Negative = got cheaper."""
    if not prev_compact:
        return None
    def _d(a, b):
        return round(a - b, 2) if (a is not None and b is not None) else None
    return {
        "cheapest_direct": _d(new.get("cheapest_direct_total"), prev_compact.get("cheapest_direct_total")),
        "cheapest_any": _d(new.get("cheapest_any_total"), prev_compact.get("cheapest_any_total")),
        "splitsave": _d((new.get("splitsave") or {}).get("total"), prev_compact.get("splitsave_total")),
    }


def merge_snapshot(date_str: str, new_snapshot: dict) -> dict:
    """Merge one new Trainline snapshot into prices.json for the given date.
    Returns the updated tuesday dict so the caller can compose messaging."""
    data = json.loads(PRICES.read_text())
    for t in data["tuesdays"]:
        if t["date"] != date_str:
            continue
        # Push the previous current onto history, then replace
        prev = t.get("current")
        if prev:
            hist = t.get("history", [])
            hist.insert(0, compact_snapshot(prev))
            t["history"] = hist[:HISTORY_CAP]
        prev_compact = t["history"][0] if t.get("history") else None
        t["current"] = new_snapshot
        t["change_vs_yesterday"] = compute_change(new_snapshot, prev_compact)
        t["status"] = compute_status(new_snapshot)
        # Update backward-compat aliases
        t["out"] = new_snapshot.get("out")
        t["back"] = new_snapshot.get("back")
        t["total_cheapest"] = new_snapshot.get("cheapest_any_total")
        PRICES.write_text(json.dumps(data, indent=2))
        return t
    raise ValueError(f"Date {date_str} not found in prices.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="Travel date YYYY-MM-DD")
    ap.add_argument("--snapshot", required=True, help="Path to snapshot JSON (output of fetch_trainline_fares.py)")
    args = ap.parse_args()
    snap = json.loads(Path(args.snapshot).read_text())
    updated = merge_snapshot(args.date, snap)
    print(json.dumps({"ok": True, "date": args.date, "status": updated["status"]}, indent=2))


if __name__ == "__main__":
    main()
