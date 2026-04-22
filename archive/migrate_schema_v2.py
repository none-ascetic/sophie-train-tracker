#!/usr/bin/env python3
"""One-shot migration: prices.json v1 (flat snapshot) → v2 (current + history).

v1 (old): each tuesday has flat keys: out, back, total_cheapest, note
v2 (new): each tuesday has:
    current  — latest snapshot { checked_at, source, out, back, cheapest_direct_total,
                                 cheapest_any_total, splitsave, ... }
    history  — array of prior snapshots (last 14). Each snapshot has the compact shape
               { checked_at, cheapest_direct_total, cheapest_any_total, splitsave_total }
    change_vs_yesterday — derived from history[0] vs current, null on first run
    note     — still top-level

Backward-compat aliases: we also keep top-level `out`, `back`, `total_cheapest` on each
tuesday so the old generate_site.py template survives the first deploy without breaking.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
PRICES = ROOT / "prices.json"

SCHEMA_VERSION = 2


def migrate_tuesday(t: dict, now_iso: str) -> dict:
    """Transform a v1 tuesday entry into v2 shape."""
    cheapest_direct = t.get("total_cheapest")  # v1 total_cheapest is direct-only
    # In v1, notes sometimes hint at cheaper via-change options; we don't try to parse.
    current = {
        "checked_at": now_iso,
        "source": "national_rail_v1_migrated",
        "out": t.get("out"),
        "out_alternatives": t.get("out_alternatives", []),
        "back": t.get("back"),
        "back_alternatives": t.get("back_alternatives", []),
        "cheapest_direct_total": cheapest_direct,
        "cheapest_any_total": cheapest_direct,  # v1 didn't distinguish
        "splitsave": {"available": False, "total": None, "savings_vs_direct": None},
    }
    return {
        "date": t["date"],
        "weeks_out": t.get("weeks_out"),
        "status": t.get("status"),
        "current": current,
        "history": [],  # no historical data in v1
        "change_vs_yesterday": None,
        "baseline_total": t.get("baseline_total", 127.00),
        "note": t.get("note", ""),
        # Backward-compat aliases — keep until generate_site.py is updated in v2:
        "out": t.get("out"),
        "out_alternatives": t.get("out_alternatives", []),
        "back": t.get("back"),
        "back_alternatives": t.get("back_alternatives", []),
        "total_cheapest": cheapest_direct,
    }


def main():
    data = json.loads(PRICES.read_text())
    if data.get("schema_version") == SCHEMA_VERSION:
        print(f"Already at schema v{SCHEMA_VERSION} — nothing to migrate.")
        return
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = {
        "schema_version": SCHEMA_VERSION,
        "run_date": data["run_date"],
        "route": data["route"],
        "constraints": data["constraints"],
        "booking_horizon_weeks": data["booking_horizon_weeks"],
        "booking_horizon_note": data["booking_horizon_note"],
        "constraint_note": data.get("constraint_note", ""),
        "sources": {
            "primary": "trainline",  # via Chrome MCP
            "fallback": "national_rail",  # via WebFetch when Chrome unavailable
        },
        "tuesdays": [migrate_tuesday(t, now_iso) for t in data["tuesdays"]],
        "not_bookable_yet": data["not_bookable_yet"],
        "summary": data.get("summary", {}),
    }
    PRICES.write_text(json.dumps(new, indent=2))
    print(f"Migrated {len(new['tuesdays'])} Tuesdays to schema v{SCHEMA_VERSION}")


if __name__ == "__main__":
    main()
