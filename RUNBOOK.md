# Daily rail price pipeline — 02:00 runbook

This is the playbook the scheduled task follows at 02:00 every day. The goal
is non-negotiable: produce a validated snapshot of 07:36 Yatton→Paddington
and 18:30 Paddington→Yatton fares for every unbooked Tuesday in
`prices.json`, plus a fresh horizon probe. No skips, no assumptions, no lazy
scrapes (per Paddy, 22 Apr 2026).

> **Read the `trainline-lookup` skill first** (at
> `/Users/paddydavies/.claude/skills/trainline-lookup/SKILL.md`, or
> `/sessions/gallant-peaceful-babbage/mnt/.claude/skills/trainline-lookup/SKILL.md`
> from the sandbox). It encodes the working Trainline URL format (URN
> location codes — NOT hash IDs; `selectedTab=train`, `splitSave=true`,
> `transportModes[]=mixed`), the current DOM selectors, and a
> date-guarded `extractor.js`. Every bullet in this runbook assumes you
> already loaded the skill — don't re-derive the URL or selectors from
> scratch.

## Sequence

1. **Read `prices.json`** — list every unbooked Tuesday (skip `booked: true`).
2. **Probe the horizon** — find the first date in `not_bookable_yet`; navigate
   Trainline for that date; check whether the URL redirects to
   `selectedTab=coach` or returns rail results. Record the outcome.
3. **Scrape every unbooked Tuesday** — for each date:
   - Navigate to the Trainline results URL with `splitSave=true`.
   - Wait for both `train-results-container-OUTWARD` and
     `train-results-container-INWARD` to populate (≥2 rows each).
   - Extract every visible row: `{dep, arr, price}` from both containers.
   - Validate: the outward list must contain a row with `dep == "07:36"` AND
     the inward list must contain a row with `dep == "18:30"`, both with
     numeric prices.
   - **If validation fails**: wait 30 seconds, reload the tab, re-extract.
     Retry up to 3 times. If the third attempt still fails, record the
     failure — do not move on silently.
4. **Write `raw_snapshot.json`** — the full capture, including every raw row
   for audit. Shape:
   ```json
   {
     "probed_at": "2026-04-22T02:05:00Z",
     "horizon_probe": {
       "probe_date": "2026-10-20",
       "bookable": false,
       "coach_redirect": true,
       "out_count": 0,
       "inw_count": 0,
       "checked_at": "2026-04-22T02:03:00Z",
       "note": "Tuesday 20 Oct — still beyond horizon"
     },
     "tuesdays": [
       {
         "date": "2026-06-09",
         "outward": [{"dep": "07:36", "arr": "09:34", "price": 86.70}, ...],
         "inward":  [{"dep": "18:30", "arr": "20:27", "price": 27.00}, ...],
         "splitsave": {"available": true, "total": 113.70, "savings_vs_direct": 0}
       },
       ...
     ]
   }
   ```
5. **Run `daily_run.py`** — it validates `raw_snapshot.json` against
   `prices.json`, updates prices only if every Tuesday passed, and writes
   `run_status.json` (`ok` or `failed`).
6. **Read `run_status.json`**:
   - `ok` → send the iMessage at 06:00 (separate task reads `pending_message.txt`).
   - `failed` → DO NOT send Sophie's iMessage. Alert Paddy using
     `paddy_alert.txt`. Yesterday's prices and message are preserved as-is.

## Key invariants

- **Sophie's constraints are non-negotiable**: 07:36 out, 18:30 back. Any
  scrape that can't locate both rows with numeric fares is a failure, even
  if cheaper trains were visible on the page.
- **Retries are automatic (up to 3 per Tuesday, 30s gap)** — this is the
  resilience layer against Trainline's occasional slow hydration or bot-
  detection friction. Beyond 3 retries, it's a real problem worth Paddy's
  attention.
- **Failures are blocking, not silent**: a hard failure on any Tuesday
  aborts Sophie's message. The rationale: a partial message is worse than
  no message — she stops trusting it.
- **Yesterday's data is always the safe fallback**: `daily_run.py` never
  overwrites `prices.json` on a failed run, so the tracker site and old
  pending message remain coherent.

## Files touched

| File | Writer | Purpose |
| --- | --- | --- |
| `raw_snapshot.json` | Scheduled task (Chrome MCP) | Raw scraped rows per Tuesday |
| `horizon_log.jsonl` | `daily_run.py` | Append-only daily horizon probe history |
| `prices.json` | `daily_run.py` | Canonical prices — only updated on `ok` |
| `pending_message.txt` | `daily_run.py` → `compose_imessage.py` | Sophie's iMessage body |
| `paddy_alert.txt` | `daily_run.py` | Paddy-only alert on `failed` |
| `run_status.json` | `daily_run.py` | Gate for the 06:00 send step |

## Failure modes by priority

1. **Trainline unreachable / bot-challenge page** — retries exhaust, hard fail.
2. **Layout changed / selectors moved** — validation fails on every Tuesday,
   hard fail. Selectors are documented in `RUNBOOK.md` and tested in
   `tests/fixtures/`.
3. **Cheap tier gone for one date** — NOT a failure. The 07:36 / 18:30 rows
   still exist, just at a higher price. Record it and let the status update.
4. **Horizon shifted backwards unexpectedly** — not a failure on its own,
   but worth logging. `horizon_log.jsonl` will show the regression.
