#!/usr/bin/env python3
"""One-shot backfill: walk git log of raw_snapshot.json and append every
unseen observation to fare_history.jsonl.

Rationale:
  We introduced fare_history.jsonl on 2026-04-24, but raw_snapshot.json has
  been committed daily since 2026-04-22. Starting the trend dataset from
  zero would throw away days of real observations. This script ingests the
  committed snapshots into the new log, idempotently.

Idempotency:
  Each fare_history row has a `run_id` (the snapshot's `probed_at`). Before
  appending, we load the existing log and skip any (run_id, travel_date)
  we've already seen. Running this script twice is safe.

Usage:
  From the Mac:
    python3 backfill_fare_history.py            # dry-run summary
    python3 backfill_fare_history.py --write    # actually write rows

Must be run on the Mac (via osascript) — the sandbox can't run git. The
script shells out to `git` in the repo directory.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import fare_history

ROOT = Path(__file__).parent


def git_commits_touching(path: str) -> list[str]:
    """List commit SHAs (oldest first) that touched the given path."""
    res = subprocess.run(
        ["git", "log", "--reverse", "--format=%H", "--", path],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def git_show_file(sha: str, path: str) -> str | None:
    """Contents of `path` at `sha`, or None if not present there."""
    res = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="Actually append rows (default is dry-run).")
    args = ap.parse_args()

    try:
        commits = git_commits_touching("raw_snapshot.json")
    except subprocess.CalledProcessError as exc:
        print(f"git log failed: {exc}", file=sys.stderr)
        return 2

    if not commits:
        print("No commits touching raw_snapshot.json — nothing to backfill.")
        return 0

    seen = fare_history.existing_run_ids()
    print(f"Scanning {len(commits)} commits; already have "
          f"{len(seen)} (run_id, travel_date) pairs in log.")

    candidate_rows: list[dict] = []
    per_commit_stats: list[tuple[str, int, int]] = []  # (sha, total, new)
    for sha in commits:
        text = git_show_file(sha, "raw_snapshot.json")
        if not text:
            continue
        try:
            snap = json.loads(text)
        except json.JSONDecodeError:
            # Early commits may have had a different shape — skip.
            continue
        rows = fare_history.observations_from_snapshot(snap)
        new_here = [
            r for r in rows
            if f"{r.get('run_id')}|{r.get('travel_date')}" not in seen
        ]
        for r in new_here:
            seen.add(f"{r.get('run_id')}|{r.get('travel_date')}")
        per_commit_stats.append((sha[:8], len(rows), len(new_here)))
        candidate_rows.extend(new_here)

    print()
    print(f"{'commit':<10} {'total':>7} {'new':>7}")
    for sha, total, new in per_commit_stats:
        print(f"{sha:<10} {total:>7} {new:>7}")
    print(f"{'TOTAL':<10} {'-':>7} {len(candidate_rows):>7}")
    print()

    if not candidate_rows:
        print("Nothing to add — log is already current.")
        return 0

    if not args.write:
        print("Dry-run — re-run with --write to append these rows.")
        return 0

    n = fare_history.append_observations(candidate_rows)
    print(f"Appended {n} rows to fare_history.jsonl.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
