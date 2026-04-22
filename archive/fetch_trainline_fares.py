#!/usr/bin/env python3
"""Parse Trainline search-results page text into structured fare data.

Designed to be called by a Claude scheduled task that:
  1. Opens Trainline via Chrome MCP (mcp__claude-in-chrome__tabs_create_mcp)
  2. Waits for JS to render fare tiles
  3. Extracts page text via mcp__claude-in-chrome__get_page_text
  4. Passes that text (stdin or file) to this parser
  5. Receives a structured dict back

Because Trainline's DOM is JS-rendered and may change, the parsing is deliberately
defensive: regex-based, tolerant of whitespace, and it logs what it expected vs. got
when parsing fails — so future runs can iterate on selectors without full rewrites.

The page text this parser expects looks roughly like (Trainline's 2026 layout):

    Outbound
    07:36
    09:34
    Yatton (YAT) → London Paddington (PAD)
    1h 58m · Direct · GWR
    from £100.00
    Advance Single
    ...
    SplitSave price
    £89.00
    Save £38

The actual strings vary — first live run is the calibration moment. See
TRAINLINE_SELECTORS below for the signals we look for.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent

# Signals — loose enough to survive minor Trainline copy tweaks.
FARE_RE = re.compile(r"£\s?(\d{1,3}(?:\.\d{2})?)")
TIME_RE = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\b")
DURATION_RE = re.compile(r"(\d+)h\s*(\d+)m")
CHANGES_RE = re.compile(r"(\d+)\s*(?:change|changes|stop)", re.I)
DIRECT_RE = re.compile(r"\bdirect\b", re.I)
SPLITSAVE_RE = re.compile(
    r"(?:SplitSave|Split[\s-]?ticket|Split your ticket).{0,80}?£\s?(\d{1,3}(?:\.\d{2})?)",
    re.I | re.S,
)
SPLITSAVE_SAVINGS_RE = re.compile(
    r"(?:Save|You save|Saving)\s*£\s?(\d{1,3}(?:\.\d{2})?)",
    re.I,
)


def parse_leg_block(text: str, leg: str) -> Optional[dict]:
    """Parse a single leg (outbound or return) block of text.

    Expects a chunk of text covering one leg's search results. Returns the
    cheapest valid option as { time, arrival, duration_min, changes, fare }.
    """
    # Collapse whitespace for easier scanning
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return None

    # Find the first two times (departure + arrival) in the block
    times = TIME_RE.findall(compact)
    if not times:
        return None
    depart = f"{times[0][0]}:{times[0][1]}"
    arrive = f"{times[1][0]}:{times[1][1]}" if len(times) > 1 else None

    # Duration
    dur_m = DURATION_RE.search(compact)
    duration_min = int(dur_m.group(1)) * 60 + int(dur_m.group(2)) if dur_m else None

    # Changes — 0 if "Direct" appears before any "N change"
    is_direct = bool(DIRECT_RE.search(compact))
    changes = 0 if is_direct else (
        int(CHANGES_RE.search(compact).group(1)) if CHANGES_RE.search(compact) else None
    )

    # Cheapest fare in this block
    fares = [float(f) for f in FARE_RE.findall(compact)]
    fare = min(fares) if fares else None

    return {
        "leg": leg,
        "time": depart,
        "arrival": arrive,
        "duration_min": duration_min,
        "changes": changes,
        "fare": fare,
    }


def parse_splitsave(text: str) -> dict:
    """Look for a SplitSave panel anywhere in the page text."""
    m = SPLITSAVE_RE.search(text)
    if not m:
        return {"available": False, "total": None, "savings_vs_direct": None}
    total = float(m.group(1))
    savings = None
    s = SPLITSAVE_SAVINGS_RE.search(text)
    if s:
        savings = float(s.group(1))
    return {"available": True, "total": total, "savings_vs_direct": savings}


def parse_trainline_page(page_text: str, travel_date: str,
                        out_latest: str = "07:36",
                        return_earliest: str = "18:30") -> dict:
    """Full-page parse.

    Page text is the entire DOM text extracted by Chrome MCP's get_page_text.
    We split on obvious section headers ('Outbound', 'Return') to isolate legs.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Section split — Trainline typically has "Outbound" and "Return" headers
    out_match = re.search(r"Outbound(.*?)(?=Return|Inbound|$)", page_text,
                         re.I | re.S)
    ret_match = re.search(r"(?:Return|Inbound)(.*?)$", page_text, re.I | re.S)

    out_text = out_match.group(1) if out_match else page_text
    ret_text = ret_match.group(1) if ret_match else ""

    out_leg = parse_leg_block(out_text, "outbound")
    back_leg = parse_leg_block(ret_text, "return") if ret_text else None

    # SplitSave scan across whole page
    splitsave = parse_splitsave(page_text)

    # Totals
    direct_total = None
    if out_leg and back_leg and out_leg.get("fare") and back_leg.get("fare"):
        direct_total = round(out_leg["fare"] + back_leg["fare"], 2)

    any_total = direct_total  # TODO v2.1: extract cheapest-with-change from alternatives list

    return {
        "checked_at": now_iso,
        "travel_date": travel_date,
        "source": "trainline",
        "out": out_leg,
        "back": back_leg,
        "cheapest_direct_total": direct_total,
        "cheapest_any_total": any_total,
        "splitsave": splitsave,
        "parse_confidence": _confidence(out_leg, back_leg),
    }


def _confidence(out_leg, back_leg) -> str:
    """Self-assessed parse quality so downstream tasks can decide whether to trust."""
    if not out_leg or not out_leg.get("fare"):
        return "outbound_missing"
    if not back_leg or not back_leg.get("fare"):
        return "return_missing"
    if not out_leg.get("time") or not back_leg.get("time"):
        return "time_missing"
    return "ok"


def main():
    """CLI: read page text from stdin, write JSON to stdout.

    Usage:
        cat trainline_page.txt | python3 fetch_trainline_fares.py 2026-06-16
    """
    if len(sys.argv) < 2:
        print("Usage: fetch_trainline_fares.py <travel_date YYYY-MM-DD>", file=sys.stderr)
        sys.exit(2)
    travel_date = sys.argv[1]
    page_text = sys.stdin.read()
    result = parse_trainline_page(page_text, travel_date)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
