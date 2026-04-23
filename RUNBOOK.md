# Daily rail price pipeline — 02:00 runbook

This is the playbook the scheduled task follows at 02:00 every day. The goal
is non-negotiable: produce a validated snapshot of 07:36 Yatton→Paddington
and 18:30 Paddington→Yatton fares for every unbooked Tuesday in
`prices.json`, plus a fresh horizon probe. No skips, no assumptions, no lazy
scrapes (per Paddy, 22 Apr 2026).

> **Load the `trainline-lookup` skill first.** It's installed in Cowork
> and available in every session. Invoke it with the Skill tool:
> `Skill({skill: "trainline-lookup"})`. It encodes the working URL
> format (URN location codes — NOT hash IDs; `selectedTab=train`,
> `splitSave=true`, `transportModes[]=mixed`), the DOM selectors
> (`train-results-container-OUTWARD`/`INWARD`, `alternative-price`),
> the date-guarded `extractor.js`, and a station URN cache
> (`stations.md`). Every scrape step in this runbook assumes the skill
> is loaded — don't re-derive any of it from scratch.
>
> **Session-agnostic paths.** The Train Tickets folder is mounted at
> `/sessions/<session>/mnt/Train Tickets/` and the session name rotates
> every Cowork run. Discover the folder via `pwd` (if CWD is inside it)
> or glob `/sessions/*/mnt/Train\ Tickets/RUNBOOK.md`. Never hardcode
> a session name. The `trainline-lookup` skill is reached via the Skill
> tool, not by path.

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
7. **Commit AND push via the Mac** (the Cowork sandbox mount is
   additive-only — it can create files inside `.git/` but cannot
   unlink them, so a sandbox-native `git commit` leaves stale
   `.git/index.lock`, `HEAD.lock`, and `tmp_obj_*` files that block
   the next run. Use the Mac, where `rm` works normally). The real
   repo lives at `/Users/paddydavies/Documents/Claude/Projects/Train Tickets`
   and the tracker at https://sophie-train-tracker.vercel.app auto-deploys
   from the GitHub remote on push. The push is load-bearing: without it,
   Sophie's morning page is yesterday's data. Via
   `mcp__Control_your_Mac__osascript`:
   ```sh
   cd "/Users/paddydavies/Documents/Claude/Projects/Train Tickets" \
     && git add prices.json raw_snapshot.json run_status.json horizon_log.jsonl pending_message.txt paddy_alert.txt 2>/dev/null; \
     if ! git diff --cached --quiet; then \
       git commit -m "Daily rail scrape $(date +%Y-%m-%d)" && git push origin main; \
     else \
       echo "no changes to commit"; \
     fi
   ```
   If the push fails (network/auth/conflict), overwrite `paddy_alert.txt`
   with the git error so the 06:00 task pages Paddy instead of sending
   Sophie a stale message. Skip commit+push only if the run was `failed`
   AND no files changed.

   > **Note on `GITHUB_TOKEN`**: a fine-grained PAT lives in `$WORK/.env`
   > as `GITHUB_TOKEN`. It is NOT used for the nightly push (see above).
   > It's kept around for GitHub API calls from the sandbox where
   > unlinking isn't involved — issues, workflow dispatches, content API,
   > etc. `.env` is gitignored; never stage or commit it.

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
