#!/usr/bin/env python3
"""Fetch National Rail fares for YAT↔PAD via ojp.nationalrail.co.uk.

Parses the embedded <script id="jsonJourney-N-N"> JSON payloads on the
times-and-fares page — far more reliable than regex over rendered markup.

Used as the fallback data source when Trainline (via Chrome MCP) is
unreachable. Output format matches fetch_trainline_fares.py snapshot shape
so update_prices.merge_snapshot() can ingest it unchanged.

Usage:
    python3 fetch_nr_fares.py 2026-04-28
"""
import concurrent.futures as cf
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent

OUT_LATEST = "07:36"
RET_EARLIEST = "18:30"
RET_WINDOW_END = "20:30"

UA = "Mozilla/5.0 (SophieTrainlineCheck)"
TIMEOUT = 25

JOURNEY_RE = re.compile(
    r'<script[^>]*id="jsonJourney-\d+-\d+"[^>]*>\s*(\{.*?\})\s*</script>',
    re.S,
)


def _iso_to_ddmmyy(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return d.strftime("%d%m%y")


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_journeys(html: str) -> list:
    """Return list of {time, arrival, duration_min, changes, fare} sorted by departure."""
    out = []
    for raw in JOURNEY_RE.findall(html):
        try:
            j = json.loads(raw)
        except json.JSONDecodeError:
            continue
        jb = j.get("jsonJourneyBreakdown") or {}
        fares = j.get("singleJsonFareBreakdowns") or []
        if not fares or not jb.get("departureTime"):
            continue
        cheapest = min((f.get("ticketPrice") for f in fares
                        if f.get("ticketPrice") is not None),
                       default=None)
        if cheapest is None:
            continue
        out.append({
            "time": jb["departureTime"],
            "arrival": jb.get("arrivalTime"),
            "duration_min": (jb.get("durationHours") or 0) * 60 + (jb.get("durationMinutes") or 0),
            "changes": jb.get("changes"),
            "fare": float(cheapest),
        })
    # De-dupe identical rows (NR renders the same journey multiple times for
    # different ticket classes/providers)
    seen = set()
    deduped = []
    for r in out:
        key = (r["time"], r["arrival"], r["fare"], r["changes"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    deduped.sort(key=lambda r: r["time"])
    return deduped


def _filter_outbound(rows: list) -> tuple:
    """Primary: cheapest direct option within window ≤ 07:36, preferring the
    latest time (closest to 07:36) on price ties. If no direct option in window,
    fall back to the cheapest row in window (may require a change)."""
    in_window = [r for r in rows if r["time"] <= OUT_LATEST]
    if not in_window:
        in_window = rows[:1]
    direct = [r for r in in_window if (r.get("changes") or 0) == 0]
    pool = direct or in_window
    # Sort by (fare asc, time desc) so cheapest + latest wins
    pool_sorted = sorted(pool, key=lambda r: (r["fare"], -_time_key(r["time"])))
    primary = pool_sorted[0] if pool_sorted else None
    alts = [r for r in in_window if r is not primary]
    return primary, alts


def _filter_return(rows: list) -> tuple:
    """Primary: cheapest direct option within window [18:30, 20:30], preferring
    the earliest time on price ties. Fall back to cheapest in window if no
    direct option."""
    in_window = [r for r in rows if RET_EARLIEST <= r["time"] <= RET_WINDOW_END]
    if not in_window:
        in_window = rows[:1]
    direct = [r for r in in_window if (r.get("changes") or 0) == 0]
    pool = direct or in_window
    pool_sorted = sorted(pool, key=lambda r: (r["fare"], _time_key(r["time"])))
    primary = pool_sorted[0] if pool_sorted else None
    alts = [r for r in in_window if r is not primary]
    return primary, alts


def _time_key(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def fetch_one_date(iso_date: str) -> dict:
    """Fetch outbound + return for a single date and assemble a snapshot."""
    ddmmyy = _iso_to_ddmmyy(iso_date)
    out_url = f"https://ojp.nationalrail.co.uk/service/timesandfares/YAT/PAD/{ddmmyy}/0530/dep"
    ret_url = f"https://ojp.nationalrail.co.uk/service/timesandfares/PAD/YAT/{ddmmyy}/1815/dep"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        out_html = _fetch(out_url)
        ret_html = _fetch(ret_url)
    except Exception as e:
        return {
            "checked_at": now_iso,
            "travel_date": iso_date,
            "source": "national_rail",
            "parse_confidence": "fetch_failed",
            "error": f"{type(e).__name__}: {e}",
        }

    out_rows = _parse_journeys(out_html)
    ret_rows = _parse_journeys(ret_html)

    if not out_rows or not ret_rows:
        return {
            "checked_at": now_iso,
            "travel_date": iso_date,
            "source": "national_rail",
            "parse_confidence": "empty_results",
            "out_rows_found": len(out_rows),
            "ret_rows_found": len(ret_rows),
        }

    out_primary, out_alts = _filter_outbound(out_rows)
    ret_primary, ret_alts = _filter_return(ret_rows)

    def _strip(row):
        return {
            "time": row["time"],
            "duration_min": row.get("duration_min"),
            "changes": row.get("changes"),
            "fare": row.get("fare"),
        }

    def _alt(row):
        return {
            "time": row["time"],
            "fare": row.get("fare"),
            "changes": row.get("changes"),
        }

    # Both totals use primary picks — matches the v1/v2 schema where "direct"
    # and "any" refer to primary-row totals (alternatives are surfaced separately
    # via out_alternatives / back_alternatives).
    direct_total = any_total = None
    if out_primary and ret_primary:
        direct_total = round(out_primary["fare"] + ret_primary["fare"], 2)
        any_total = direct_total

    return {
        "checked_at": now_iso,
        "travel_date": iso_date,
        "source": "national_rail",
        "out": _strip(out_primary) if out_primary else None,
        "out_alternatives": [_alt(r) for r in out_alts[:6]],
        "back": _strip(ret_primary) if ret_primary else None,
        "back_alternatives": [_alt(r) for r in ret_alts[:6]],
        "cheapest_direct_total": direct_total,
        "cheapest_any_total": any_total,
        "splitsave": {"available": False, "total": None, "savings_vs_direct": None},
        "parse_confidence": "ok",
        "_note_on_source": "NR fare floor — Trainline unreachable. Tap through Trainline app for SplitSave.",
    }


def fetch_many(iso_dates: list, max_workers: int = 6) -> dict:
    """Fetch all dates in parallel. Returns { date: snapshot }."""
    results = {}
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(fetch_one_date, d): d for d in iso_dates}
        for f in cf.as_completed(futs):
            d = futs[f]
            try:
                results[d] = f.result()
            except Exception as e:
                results[d] = {"parse_confidence": "exception",
                              "error": f"{type(e).__name__}: {e}",
                              "travel_date": d,
                              "source": "national_rail"}
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: fetch_nr_fares.py <date1> [date2 ...]", file=sys.stderr)
        sys.exit(2)
    dates = sys.argv[1:]
    if len(dates) == 1:
        print(json.dumps(fetch_one_date(dates[0]), indent=2))
    else:
        print(json.dumps(fetch_many(dates), indent=2))


if __name__ == "__main__":
    main()
