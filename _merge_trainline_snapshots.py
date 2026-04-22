#!/usr/bin/env python3
"""One-off: fold trainline_snapshots/*.json into prices.json via update_prices.merge_snapshot."""
import json
from datetime import datetime, timezone
from pathlib import Path

import update_prices

ROOT = Path(__file__).parent
SNAP_DIR = ROOT / "trainline_snapshots"

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(snap: dict) -> dict:
    """Map our compact trainline snapshot → the schema update_prices expects."""
    date = snap["date"]
    train_tab = snap.get("train_tab_price")
    total_sel = snap.get("total_selected")
    source = snap.get("source", "trainline")

    # Special-case 2026-04-28 (we captured full in-window detail)
    if date == "2026-04-28" and "outbound_in_window" in snap:
        out_opts = snap["outbound_in_window"]
        ret_opts = [r for r in snap["return_in_window"] if r.get("splitsave_single") is not None]
        cheapest_out = min(out_opts, key=lambda x: x["splitsave_single"])
        cheapest_back = min(ret_opts, key=lambda x: x["splitsave_single"])
        total_in_window = round(cheapest_out["splitsave_single"] + cheapest_back["splitsave_single"], 2)
        return {
            "checked_at": NOW,
            "travel_date": date,
            "source": source,
            "out": {
                "time": cheapest_out["dep"],
                "arrival": cheapest_out["arr"],
                "duration_min": None,
                "changes": cheapest_out.get("changes", 0),
                "fare": cheapest_out["splitsave_single"],
            },
            "back": {
                "time": cheapest_back["dep"],
                "arrival": cheapest_back["arr"],
                "duration_min": None,
                "changes": cheapest_back.get("changes", 0),
                "fare": cheapest_back["splitsave_single"],
            },
            "cheapest_direct_total": total_in_window,
            "cheapest_any_total": total_in_window,
            "splitsave": {
                "available": True,
                "total": total_in_window,
                "savings_vs_direct": 49.30,  # Trainline showed Save £49.30 on £92.70 single
            },
            "parse_confidence": "ok",
            "_note_on_source": "Captured via Trainline + SplitSave. In-window only (07:06/07:36 out, 19:30 back).",
        }

    # Other dates: only tab price + total. Use tab (which is Trainline's headline cheapest incl SplitSave).
    return {
        "checked_at": NOW,
        "travel_date": date,
        "source": source,
        "out": None,
        "back": None,
        "cheapest_direct_total": train_tab,
        "cheapest_any_total": train_tab,
        "splitsave": {
            "available": True,
            "total": train_tab,
            "savings_vs_direct": None,
        },
        "parse_confidence": "partial",
        "_note_on_source": f"Trainline auto-selected cheapest (£{train_tab} tab, £{total_sel} with fees). Time-window constraints not yet verified.",
    }


def main():
    snap_files = sorted(SNAP_DIR.glob("*.json"))
    results = []
    for p in snap_files:
        snap = json.loads(p.read_text())
        date = snap["date"]
        canon = canonical(snap)
        updated = update_prices.merge_snapshot(date, canon)
        results.append({
            "date": date,
            "cheapest_any": canon["cheapest_any_total"],
            "status": updated["status"],
        })
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
