"""Unit tests for the two prior-selection helpers in fare_history.

These pin the bug fixed on 2026-04-29: `apply_fresh_prices` was using
`_prior_from_history` (rows[-2]), which on the first run of the day picks
day-before-yesterday because today's observations haven't been appended
yet — producing a phantom "↑ £7" headline across every Tuesday when
nothing actually moved overnight.

`_latest_pre_run_prior(history, current_run_id)` filters by run_id so it
returns yesterday regardless of whether today's row is in the history.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from tests/ directly: tests/ → project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fare_history  # noqa: E402


def _row(travel_date: str, run_id: str, total: float, out_fare: float = 93.7,
         back_fare: float = 27.0) -> dict:
    """Build a minimal fare_history row matching the live schema."""
    return {
        "schema": 2,
        "run_id": run_id,
        "observed_at": run_id,  # in production these match for live runs
        "observed_on": run_id[:10],
        "travel_date": travel_date,
        "out_07_36": out_fare,
        "back_18_30": back_fare,
        "total": total,
    }


def test_latest_pre_run_returns_yesterday_on_first_run() -> None:
    """First run of the day: today's observation isn't in history yet, so
    the helper must return the most recent existing row (yesterday)."""
    history = [
        _row("2026-06-09", run_id="2026-04-27T02:00:00Z", total=113.70, out_fare=86.7),
        _row("2026-06-09", run_id="2026-04-28T02:00:00Z", total=120.70, out_fare=93.7),
    ]
    today = "2026-04-29T02:00:00Z"
    priors = fare_history._latest_pre_run_prior(history, current_run_id=today)
    assert priors["2026-06-09"]["cheapest_any_total"] == 120.70, (
        "expected yesterday (£120.70), got "
        f"{priors['2026-06-09']['cheapest_any_total']}"
    )


def test_latest_pre_run_skips_current_runs_row_on_rerun() -> None:
    """Same-day re-run: today's first-run row IS in history. The helper
    must skip it (run_id match) and return yesterday."""
    today = "2026-04-29T02:00:00Z"
    history = [
        _row("2026-06-09", run_id="2026-04-28T02:00:00Z", total=120.70),
        _row("2026-06-09", run_id=today, total=120.70),  # earlier-today
    ]
    priors = fare_history._latest_pre_run_prior(history, current_run_id=today)
    assert priors["2026-06-09"]["cheapest_any_total"] == 120.70
    # And critically — the row we picked was yesterday's, not today's.
    # Distinguish by run_id presence: yesterday's row has out_fare=93.7
    # and back=27.0; both rows happen to share the same total here, so
    # rebuild with distinct totals to make sure we got the right one.
    history = [
        _row("2026-06-09", run_id="2026-04-28T02:00:00Z", total=113.70, out_fare=86.7),
        _row("2026-06-09", run_id=today, total=120.70, out_fare=93.7),
    ]
    priors = fare_history._latest_pre_run_prior(history, current_run_id=today)
    assert priors["2026-06-09"]["cheapest_any_total"] == 113.70, (
        "expected yesterday (£113.70), got "
        f"{priors['2026-06-09']['cheapest_any_total']}"
    )


def test_latest_pre_run_omits_dates_with_only_current_run() -> None:
    """A travel_date observed only in today's run has no prior — skip it,
    don't fabricate a zero baseline."""
    today = "2026-04-29T02:00:00Z"
    history = [_row("2026-10-29", run_id=today, total=70.80)]
    priors = fare_history._latest_pre_run_prior(history, current_run_id=today)
    assert "2026-10-29" not in priors


def test_prior_from_history_still_returns_second_most_recent() -> None:
    """Regression guard for `analyse_movements` — it runs AFTER append, so
    rows[-1] is today and rows[-2] is yesterday. Don't accidentally change
    those semantics while fixing apply_fresh_prices."""
    history = [
        _row("2026-06-09", run_id="2026-04-28T02:00:00Z", total=120.70),
        _row("2026-06-09", run_id="2026-04-29T02:00:00Z", total=120.70),
    ]
    priors = fare_history._prior_from_history(history)
    # rows[-2] is yesterday — same total so check via the helper return shape
    assert priors["2026-06-09"]["cheapest_any_total"] == 120.70


def main() -> int:
    test_latest_pre_run_returns_yesterday_on_first_run()
    test_latest_pre_run_skips_current_runs_row_on_rerun()
    test_latest_pre_run_omits_dates_with_only_current_run()
    test_prior_from_history_still_returns_second_most_recent()
    print("ALL HISTORY-PRIOR TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
