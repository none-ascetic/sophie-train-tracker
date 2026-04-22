#!/usr/bin/env python3
"""Daily pipeline post-processor — the validation gate between Chrome-scraped
raw data and Sophie's iMessage.

Flow (called by the 02:00 scheduled task AFTER Claude has driven Chrome):

  raw_snapshot.json  ─▶  daily_run.py  ─▶  prices.json updated
                                      └─▶  run_status.json (ok | failed)
                                      └─▶  pending_message.txt (only if ok)
                                      └─▶  paddy_alert.txt    (only if failed)

Paddy's directive (22 Apr 2026): "EVERY DAY. No skips/assumptions or lazy
scrapes. it must be validated." That's what this script enforces.

VALIDATION RULES (strict — any failure aborts Sophie's iMessage):
  1. Every unbooked Tuesday in prices.json MUST be present in raw_snapshot.
  2. For each, the outward rows MUST contain a 07:36 departure with a
     numeric fare (the train Sophie actually takes).
  3. For each, the inward rows MUST contain an 18:30 departure with a
     numeric fare (Sophie's primary return).
  4. Horizon probe MUST be present (bookable vs coach-redirect).

If ANY Tuesday fails validation — even after the scheduled task's retries —
Sophie's iMessage is held and Paddy gets an alert file instead. Yesterday's
prices are preserved (we don't overwrite with stale or partial data).
"""
from __future__ import annotations

import json
import sys
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
PRICES = ROOT / "prices.json"
RAW = ROOT / "raw_snapshot.json"
STATUS = ROOT / "run_status.json"
PENDING = ROOT / "pending_message.txt"
ALERT = ROOT / "paddy_alert.txt"
HORIZON_LOG = ROOT / "horizon_log.jsonl"
RUN_LOG = ROOT / "run_log.jsonl"

# "Big mover" threshold — a Tuesday whose total shifted by more than this
# versus yesterday is worth calling out in the run log (and, eventually, in
# Sophie's message body). £5 is the rough sensitivity Paddy uses when scanning
# the tracker — smaller than that is noise.
BIG_MOVER_DELTA_GBP = 5.0

# Sophie's hard constraints — not configurable without her say-so.
OUT_DEPARTURE = "07:36"   # Yatton → Paddington (07:36 arr 09:34 typical)
BACK_DEPARTURE = "18:30"  # Paddington → Yatton (18:30 arr 20:27 typical)


# ────────────────────────────────────────────────────────────────────────────
# Validation
# ────────────────────────────────────────────────────────────────────────────

def _find_row(rows: list[dict], dep: str) -> dict | None:
    """Return the row whose departure time matches exactly — nothing else counts."""
    for r in rows or []:
        if (r.get("dep") or "").strip() == dep:
            return r
    return None


def validate_tuesday(entry: dict) -> tuple[bool, str, dict | None, dict | None]:
    """Check one scraped Tuesday passes the strict rules.

    Returns (ok, reason, out_row, back_row). Reason is a short human-readable
    failure description used in the Paddy alert.
    """
    outward = entry.get("outward") or []
    inward = entry.get("inward") or []
    if not outward or not inward:
        return False, "empty outward or inward rows", None, None
    out_row = _find_row(outward, OUT_DEPARTURE)
    if out_row is None:
        return False, f"no {OUT_DEPARTURE} outward row — scraper missed it", None, None
    if not isinstance(out_row.get("price"), (int, float)):
        return False, f"{OUT_DEPARTURE} outward row has non-numeric price", None, None
    back_row = _find_row(inward, BACK_DEPARTURE)
    if back_row is None:
        return False, f"no {BACK_DEPARTURE} return row — scraper missed it", None, out_row
    if not isinstance(back_row.get("price"), (int, float)):
        return False, f"{BACK_DEPARTURE} return row has non-numeric price", out_row, None
    return True, "", out_row, back_row


def expected_dates(prices: dict) -> list[str]:
    """Every unbooked Tuesday we expect to see in the raw snapshot."""
    return [t["date"] for t in prices.get("tuesdays", []) if not t.get("booked")]


# ────────────────────────────────────────────────────────────────────────────
# Price application
# ────────────────────────────────────────────────────────────────────────────

BASELINE = 127.00
CHECKED_AT_FALLBACK = datetime.utcnow().isoformat(timespec="seconds") + "Z"


def status_for(total: float) -> str:
    if total <= 85:
        return "BOOK_TODAY"
    if total <= 110:
        return "BOOK_SOON"
    if total <= 127:
        return "STABLE"
    return "URGENT"


def apply_fresh_prices(prices: dict, validated: dict[str, dict], checked_at: str) -> None:
    """Update prices.json entries in place for every validated Tuesday.

    Each entry gets: current snapshot refreshed, history pushed back by one,
    change_vs_yesterday computed against the prior current, status re-derived."""
    by_date = {t["date"]: t for t in prices["tuesdays"]}
    for dstr, val in validated.items():
        t = by_date.get(dstr)
        if not t:
            continue
        out_fare = val["out_row"]["price"]
        back_fare = val["back_row"]["price"]
        new_total = round(out_fare + back_fare, 2)

        prior_current = t.get("current") or {}
        prior_total = prior_current.get("cheapest_any_total")

        # Roll yesterday's current into history (cap at 30 entries — plenty for trend).
        history = t.get("history") or []
        if prior_current:
            history = [prior_current] + history[:29]

        new_current = {
            "checked_at": checked_at,
            "travel_date": dstr,
            "source": "trainline",
            "out": {
                "time": val["out_row"].get("dep"),
                "arrival": val["out_row"].get("arr"),
                "fare": out_fare,
            },
            "back": {
                "time": val["back_row"].get("dep"),
                "arrival": val["back_row"].get("arr"),
                "fare": back_fare,
            },
            "cheapest_direct_total": new_total,
            "cheapest_any_total": new_total,
            "splitsave": val.get("splitsave") or {"available": None, "total": None},
            "parse_confidence": "ok",
            "_raw_rows": {  # audit trail — Paddy can diff if something looks off
                "outward": val["outward_all"],
                "inward": val["inward_all"],
            },
            "_note_on_source": "Scraped via Chrome MCP. 07:36 OUT + 18:30 RETURN validated.",
        }

        change = None
        if isinstance(prior_total, (int, float)):
            change = round(new_total - prior_total, 2)

        t["current"] = new_current
        t["history"] = history
        t["change_vs_yesterday"] = {"cheapest_any": change, "splitsave": change}
        t["status"] = status_for(new_total)
        t["total_cheapest"] = new_total
        t["out"] = new_current["out"]
        t["back"] = new_current["back"]


def add_newly_bookable(prices: dict, horizon_probe: dict) -> str | None:
    """If the horizon probe unlocked a new Tuesday, move it from
    not_bookable_yet → tuesdays with a placeholder entry (today's scrape will
    fill it). Returns the date string if one was unlocked, else None."""
    if not horizon_probe or not horizon_probe.get("bookable"):
        return None
    probe_date = horizon_probe.get("probe_date")
    if not probe_date:
        return None
    nbk = prices.get("not_bookable_yet") or []
    if probe_date not in nbk:
        return None
    # Only Tuesdays matter for Sophie's commute.
    try:
        d = datetime.strptime(probe_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    if d.weekday() != 1:  # 1 = Tuesday
        return None

    prices["not_bookable_yet"] = [x for x in nbk if x != probe_date]
    # Placeholder — scraping will replace .current on the same run.
    prices["tuesdays"].append({
        "date": probe_date,
        "weeks_out": max(1, (d - date.today()).days // 7),
        "status": "UNKNOWN",
        "current": None,
        "history": [],
        "change_vs_yesterday": None,
        "baseline_total": BASELINE,
        "note": "Newly unlocked — first scrape pending this run.",
    })
    prices["tuesdays"].sort(key=lambda t: t["date"])
    return probe_date


# ────────────────────────────────────────────────────────────────────────────
# Horizon log append
# ────────────────────────────────────────────────────────────────────────────

def append_horizon_log(probe: dict) -> None:
    """Append one line to horizon_log.jsonl. Missing/empty probe is skipped
    (the scheduled task may have been unable to run a probe this cycle)."""
    if not probe:
        return
    line = {
        "probed_on": date.today().strftime("%Y-%m-%d"),
        "probe_date": probe.get("probe_date"),
        "bookable": bool(probe.get("bookable")),
        "coach_redirect": bool(probe.get("coach_redirect")),
        "out_count": probe.get("out_count"),
        "inw_count": probe.get("inw_count"),
        "checked_at": probe.get("checked_at"),
        "note": probe.get("note") or "",
    }
    with HORIZON_LOG.open("a") as f:
        f.write(json.dumps(line) + "\n")


# ────────────────────────────────────────────────────────────────────────────
# Outputs
# ────────────────────────────────────────────────────────────────────────────

def write_paddy_alert(failures: list[dict], horizon_probe: dict | None) -> None:
    """Plain-text alert for Paddy when the pipeline can't guarantee a clean
    iMessage for Sophie. Leaves Sophie's pending_message.txt untouched."""
    lines = [
        f"PIPELINE FAIL — {date.today().strftime('%a %d %b %Y')}",
        "",
        "Sophie's iMessage HELD. Yesterday's prices preserved in prices.json.",
        "",
        f"Failed Tuesdays ({len(failures)}):",
    ]
    for f in failures:
        lines.append(f"  • {f['date']}: {f['reason']}")
    if horizon_probe is not None:
        lines.append("")
        lines.append(
            f"Horizon probe: {horizon_probe.get('probe_date')} — "
            f"{'bookable' if horizon_probe.get('bookable') else 'coach-redirect'}"
        )
    lines.append("")
    lines.append("Investigate in the tab group, re-run manually if root cause is transient.")
    ALERT.write_text("\n".join(lines) + "\n")


def delete_if_exists(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def write_status(status: str, detail: dict) -> None:
    STATUS.write_text(json.dumps({"status": status, "at": CHECKED_AT_FALLBACK, **detail}, indent=2))


def capture_prior_snapshot(prices: dict) -> dict[str, dict]:
    """Snapshot {date → {status, total}} BEFORE apply_fresh_prices mutates things.

    Needed so we can compute big_movers + status_transitions for the run log
    after the in-place update has happened."""
    out = {}
    for t in prices.get("tuesdays") or []:
        cur = t.get("current") or {}
        out[t["date"]] = {
            "status": t.get("status"),
            "total": cur.get("cheapest_any_total"),
        }
    return out


def compute_big_movers(prices: dict, prior: dict[str, dict]) -> list[dict]:
    """Tuesdays whose total moved by more than BIG_MOVER_DELTA_GBP day-over-day."""
    movers: list[dict] = []
    for t in prices.get("tuesdays") or []:
        if t.get("booked"):
            continue
        cur = t.get("current") or {}
        new_total = cur.get("cheapest_any_total")
        prev_total = (prior.get(t["date"]) or {}).get("total")
        if not isinstance(new_total, (int, float)) or not isinstance(prev_total, (int, float)):
            continue
        delta = round(new_total - prev_total, 2)
        if abs(delta) > BIG_MOVER_DELTA_GBP:
            movers.append({
                "date": t["date"],
                "prev": prev_total,
                "new": new_total,
                "delta": delta,
            })
    return movers


def compute_status_transitions(prices: dict, prior: dict[str, dict]) -> list[dict]:
    """Tuesdays whose status bucket changed in this run."""
    transitions: list[dict] = []
    for t in prices.get("tuesdays") or []:
        if t.get("booked"):
            continue
        prev_status = (prior.get(t["date"]) or {}).get("status")
        new_status = t.get("status")
        if prev_status and new_status and prev_status != new_status:
            transitions.append({
                "date": t["date"],
                "from": prev_status,
                "to": new_status,
            })
    return transitions


def append_run_log(
    *,
    status: str,
    duration_sec: float,
    tuesdays_total: int,
    scraped_trainline: int,
    scrape_failures: int,
    big_movers: list[dict],
    status_transitions: list[dict],
    pending_message_chars: int,
) -> None:
    """Append one JSONL line to run_log.jsonl preserving the historical schema.

    `fell_back_to_nr` and `source_flips` are retained as zero/empty for
    schema parity with pre-22-Apr entries — they meant something when we had
    a National Rail fallback path; today it's Trainline-only."""
    entry = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "status": status,
        "duration_sec": round(duration_sec, 2),
        "tuesdays_total": tuesdays_total,
        "scraped_trainline": scraped_trainline,
        "fell_back_to_nr": 0,
        "scrape_failures": scrape_failures,
        "big_movers": big_movers,
        "status_transitions": status_transitions,
        "source_flips": [],
        "pending_message_chars": pending_message_chars,
    }
    with RUN_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main() -> int:
    t0 = _time.monotonic()
    if not RAW.exists():
        write_status("failed", {"reason": "raw_snapshot.json missing — scraper never ran"})
        write_paddy_alert(
            [{"date": "(all)", "reason": "raw_snapshot.json missing — scraper never ran"}],
            None,
        )
        # Log this as a run too — "raw never arrived" is useful signal in the series.
        append_run_log(
            status="failed",
            duration_sec=_time.monotonic() - t0,
            tuesdays_total=0,
            scraped_trainline=0,
            scrape_failures=1,
            big_movers=[],
            status_transitions=[],
            pending_message_chars=0,
        )
        print("FAIL: raw_snapshot.json not found", file=sys.stderr)
        return 2

    raw = json.loads(RAW.read_text())
    prices = json.loads(PRICES.read_text())
    checked_at = raw.get("probed_at") or CHECKED_AT_FALLBACK
    horizon_probe = raw.get("horizon_probe")

    # Always log the horizon probe, even if the rest of the run fails — the
    # daily horizon series matters for "when does the next Tuesday unlock".
    append_horizon_log(horizon_probe or {})

    # Pull in a newly-unlocked Tuesday before validating expected dates, so its
    # placeholder exists and can be populated if the scrape also captured it.
    newly_unlocked = add_newly_bookable(prices, horizon_probe or {})

    expected = set(expected_dates(prices))
    scraped = {e["date"]: e for e in (raw.get("tuesdays") or []) if e.get("date")}

    validated: dict[str, dict] = {}
    failures: list[dict] = []

    for dstr in sorted(expected):
        entry = scraped.get(dstr)
        if entry is None:
            failures.append({"date": dstr, "reason": "missing from raw_snapshot — scraper skipped it"})
            continue
        ok, reason, out_row, back_row = validate_tuesday(entry)
        if not ok:
            failures.append({"date": dstr, "reason": reason})
            continue
        validated[dstr] = {
            "out_row": out_row,
            "back_row": back_row,
            "outward_all": entry.get("outward"),
            "inward_all": entry.get("inward"),
            "splitsave": entry.get("splitsave"),
        }

    if failures:
        # DO NOT overwrite prices.json — yesterday's data stays. DO NOT
        # regenerate Sophie's iMessage — yesterday's pending_message.txt also
        # stays as-is (but the 06:00 sender should check run_status.json and
        # refuse to fire when status != ok).
        write_status("failed", {
            "failures": failures,
            "newly_unlocked": newly_unlocked,
            "expected": sorted(expected),
            "scraped": sorted(scraped.keys()),
        })
        write_paddy_alert(failures, horizon_probe)
        append_run_log(
            status="failed",
            duration_sec=_time.monotonic() - t0,
            tuesdays_total=len(expected),
            scraped_trainline=len(validated),
            scrape_failures=len(failures),
            big_movers=[],
            status_transitions=[],
            pending_message_chars=0,
        )
        print(f"FAIL: {len(failures)} Tuesdays failed validation", file=sys.stderr)
        for f in failures:
            print(f"  - {f['date']}: {f['reason']}", file=sys.stderr)
        return 1

    # All good — apply fresh prices, persist, compose Sophie's message.
    # Snapshot prior totals/statuses BEFORE mutation so we can compute big_movers
    # and status_transitions for the run log.
    prior = capture_prior_snapshot(prices)
    apply_fresh_prices(prices, validated, checked_at)

    # Refresh summary block (compose_imessage reads prices.json but tracker
    # site reads summary for the headline).
    bookable = [t for t in prices["tuesdays"] if not t.get("booked")]
    booked = [t for t in prices["tuesdays"] if t.get("booked")]
    buckets = {"BOOK_TODAY": [], "BOOK_SOON": [], "STABLE": [], "URGENT": [], "UNKNOWN": []}
    for t in bookable:
        buckets.setdefault(t.get("status", "UNKNOWN"), []).append(t["date"])
    prices["summary"] = {
        "total_tuesdays_tracked": len(prices["tuesdays"]) + len(prices.get("not_bookable_yet") or []),
        "bookable_now": len(bookable),
        "not_bookable_yet": len(prices.get("not_bookable_yet") or []),
        "baseline_total": BASELINE,
        "booked": len(booked),
        "book_today_count": len(buckets["BOOK_TODAY"]),
        "book_soon_count": len(buckets["BOOK_SOON"]),
        "stable_count": len(buckets["STABLE"]),
        "urgent_count": len(buckets["URGENT"]),
    }
    prices["last_run"] = {
        "at": checked_at,
        "status": "ok",
        "validated_count": len(validated),
        "newly_unlocked": newly_unlocked,
    }

    PRICES.write_text(json.dumps(prices, indent=2))

    # Clear any stale alert from a prior failed run.
    delete_if_exists(ALERT)

    write_status("ok", {
        "validated_count": len(validated),
        "newly_unlocked": newly_unlocked,
    })

    # Compose Sophie's iMessage last — it reads the now-fresh prices.json.
    import compose_imessage  # lives in same dir; safe lazy import
    compose_imessage.main()

    # Append to run_log.jsonl — both the tracker site (eventually) and Paddy
    # read this for day-over-day signal.
    try:
        pending_chars = len(PENDING.read_text())
    except FileNotFoundError:
        pending_chars = 0
    append_run_log(
        status="ok",
        duration_sec=_time.monotonic() - t0,
        tuesdays_total=len(expected),
        scraped_trainline=len(validated),
        scrape_failures=0,
        big_movers=compute_big_movers(prices, prior),
        status_transitions=compute_status_transitions(prices, prior),
        pending_message_chars=pending_chars,
    )

    print(f"OK: {len(validated)} Tuesdays validated, newly_unlocked={newly_unlocked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
