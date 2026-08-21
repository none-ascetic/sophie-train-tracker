"""Smoke test for daily_run.py — runs two scenarios:

1. happy_path: all 19 Tuesdays validate → prices.json updated, pending_message
   refreshed, run_status=ok.
2. missing_row: one Tuesday's 18:30 row is missing → run_status=failed,
   prices.json untouched, paddy_alert.txt written.

The test works on copies of the real files in a temp dir, so production data
stays untouched.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent  # Train Tickets/


def build_valid_snapshot(prices: dict) -> dict:
    """One synthetic scrape row per unbooked Tuesday, using each entry's
    existing current prices so the validated run matches reality."""
    # Must be "now" — daily_run rejects a raw_snapshot older than 20h, so a
    # hardcoded date silently rots the fixture into a permanent failure.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = {
        "probed_at": now,
        "horizon_probe": {
            "probe_date": "2026-10-20",
            "bookable": False,
            "coach_redirect": True,
            "out_count": 0,
            "inw_count": 0,
            "checked_at": now,
            "note": "smoke test probe",
        },
        "tuesdays": [],
    }
    for t in prices["tuesdays"]:
        if t.get("booked"):
            continue
        out_fare = (t.get("current") or {}).get("out", {}).get("fare", 86.70)
        back_fare = (t.get("current") or {}).get("back", {}).get("fare", 27.00)
        snap["tuesdays"].append({
            "date": t["date"],
            "outward": [
                {"dep": "07:36", "arr": "09:34", "price": out_fare},
                {"dep": "08:39", "arr": "10:39", "price": out_fare + 5},
            ],
            "inward": [
                {"dep": "18:30", "arr": "20:27", "price": back_fare},
                {"dep": "19:30", "arr": "21:27", "price": back_fare},
            ],
            "splitsave": {"available": True, "total": round(out_fare + back_fare, 2)},
        })
    return snap


def run_in_tempdir(snapshot: dict) -> tuple[int, dict]:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # daily_run imports fare_history and generate_site at module level, so
        # the temp dir needs them too or the run dies before it starts.
        for name in (
            "prices.json",
            "compose_imessage.py",
            "daily_run.py",
            "fare_history.py",
            "generate_site.py",
        ):
            shutil.copy(SRC / name, td / name)
        (td / "horizon_log.jsonl").write_text("")
        (td / "raw_snapshot.json").write_text(json.dumps(snapshot))

        res = subprocess.run(
            [sys.executable, "daily_run.py"],
            cwd=td, capture_output=True, text=True,
        )
        run_log_path = td / "run_log.jsonl"
        artifacts = {
            "stdout": res.stdout,
            "stderr": res.stderr,
            "returncode": res.returncode,
            "status": json.loads((td / "run_status.json").read_text()) if (td / "run_status.json").exists() else None,
            "alert": (td / "paddy_alert.txt").read_text() if (td / "paddy_alert.txt").exists() else None,
            "pending": (td / "pending_message.txt").read_text() if (td / "pending_message.txt").exists() else None,
            "horizon_log": (td / "horizon_log.jsonl").read_text(),
            "run_log": run_log_path.read_text() if run_log_path.exists() else None,
            "prices": json.loads((td / "prices.json").read_text()),
        }
        return res.returncode, artifacts


def main() -> int:
    prices = json.loads((SRC / "prices.json").read_text())

    # ── happy path ──────────────────────────────────────────────────────
    snap_ok = build_valid_snapshot(prices)
    rc_ok, art_ok = run_in_tempdir(snap_ok)
    assert rc_ok == 0, f"happy path returncode {rc_ok}, stderr={art_ok['stderr']}"
    assert art_ok["status"]["status"] == "ok", f"expected ok, got {art_ok['status']}"
    assert art_ok["alert"] is None, "alert file should not exist on happy path"
    assert art_ok["pending"] and "Morning Soph" in art_ok["pending"], "pending message missing"
    assert len(art_ok["horizon_log"].splitlines()) == 1, "horizon log should have one entry"
    assert art_ok["run_log"], "run_log.jsonl should have been written on ok"
    happy_log = json.loads(art_ok["run_log"].strip().splitlines()[-1])
    assert happy_log["status"] == "ok", f"run_log status should be ok, got {happy_log['status']}"
    assert happy_log["scrape_failures"] == 0, "happy path should have 0 failures"
    assert happy_log["tuesdays_total"] > 0, "happy path should record Tuesdays"
    assert happy_log["pending_message_chars"] > 0, "happy path should have pending chars"
    print("HAPPY PATH: OK")
    print("  run_status:", art_ok["status"])
    print("  pending preview:", art_ok["pending"].split('\n')[0])
    print("  run_log last entry:", {k: happy_log[k] for k in ("status", "scraped_trainline", "scrape_failures", "big_movers", "status_transitions")})

    # ── poisoned basket ─────────────────────────────────────────────────
    # Regression for 2026-08-20: the step-4 click-through priced Trainline's
    # auto-selected cheapest rows instead of Sophie's 07:36/18:30, and the
    # resulting £54 total reached her message as "cheapest unbooked". The run
    # must still succeed (step 4 is non-blocking) but the phantom total and
    # the premium derived from it must both be discarded.
    snap_poison = build_valid_snapshot(prices)
    target = snap_poison["tuesdays"][0]
    real_total = target["splitsave"]["total"]
    target["splitsave"] = {"available": True, "total": round(real_total - 16.8, 2)}
    target["twox_advance_premium"] = 0.0
    rc_p, art_p = run_in_tempdir(snap_poison)
    assert rc_p == 0, f"poisoned basket should not fail the run, rc={rc_p}"
    assert art_p["status"]["status"] == "ok", "step 4 is non-blocking — run stays ok"
    poisoned_entry = next(
        t for t in art_p["prices"]["tuesdays"] if t["date"] == target["date"]
    )
    ss = poisoned_entry["current"]["splitsave"]
    assert ss["total"] is None, f"phantom basket total leaked into prices.json: {ss}"
    assert poisoned_entry["current"].get("_splitsave_note"), "discard should be recorded"
    # The validated legs are untouched — they come from the results page.
    assert poisoned_entry["current"]["cheapest_any_total"] == real_total, (
        "validated leg total must survive a bad basket"
    )
    assert str(real_total) in art_p["pending"] or "£" in art_p["pending"]
    print("POISONED BASKET: OK")
    print("  discarded:", poisoned_entry["current"]["_splitsave_note"])

    # ── missing row ─────────────────────────────────────────────────────
    snap_bad = build_valid_snapshot(prices)
    # Remove the 18:30 row from the first Tuesday.
    first_tue = snap_bad["tuesdays"][0]
    first_tue["inward"] = [r for r in first_tue["inward"] if r["dep"] != "18:30"]
    rc_bad, art_bad = run_in_tempdir(snap_bad)
    assert rc_bad == 1, f"bad path returncode {rc_bad}, stderr={art_bad['stderr']}"
    assert art_bad["status"]["status"] == "failed", f"expected failed, got {art_bad['status']}"
    assert art_bad["alert"] and "18:30" in art_bad["alert"], f"alert should mention 18:30: {art_bad['alert']}"
    # pending_message should NOT have been overwritten — it doesn't exist in tempdir at all.
    assert art_bad["pending"] is None, "pending should not be regenerated on failure"
    bad_log = json.loads(art_bad["run_log"].strip().splitlines()[-1])
    assert bad_log["status"] == "failed", f"failed path should log status=failed, got {bad_log['status']}"
    assert bad_log["scrape_failures"] >= 1, "failed path should record at least one failure"
    assert bad_log["pending_message_chars"] == 0, "failed path must not report pending chars"
    print("MISSING ROW: OK")
    print("  run_status:", art_bad["status"]["status"])
    print("  alert first line:", art_bad["alert"].split('\n')[0])
    print("  run_log last entry:", {k: bad_log[k] for k in ("status", "scraped_trainline", "scrape_failures")})

    # ── missing date entirely ──────────────────────────────────────────
    snap_gone = build_valid_snapshot(prices)
    snap_gone["tuesdays"] = snap_gone["tuesdays"][1:]  # drop first Tuesday
    rc_gone, art_gone = run_in_tempdir(snap_gone)
    assert rc_gone == 1, f"gone path returncode {rc_gone}"
    assert "missing from raw_snapshot" in art_gone["alert"], art_gone["alert"]
    print("MISSING DATE: OK")

    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
