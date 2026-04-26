# Sophie rail-booker · daily pipeline runbook

**Audience**: any Claude (scheduled or manual) executing the Yatton ↔ Paddington daily scrape. This doc is the single source of truth for the pipeline — what runs, in what order, under what failure policy. The scheduled-task prompt at `scheduled_task_prompt.md` is a thin wrapper that points here.

**Goal**: produce a validated snapshot of the 07:36 outward and 18:30 return fares for every unbooked Tuesday in `prices.json`, plus a fresh horizon probe, and deploy the tracker. No skips, no assumptions, no lazy scrapes.

---

## Prerequisites

1. **Trainline-lookup skill** must be installed in Cowork. Load it at the start of every run: `Skill({skill: "trainline-lookup"})`. It owns the scrape mechanics (URL format, URN codes, DOM selectors, skeleton guard, extractor). Do not re-derive any of that here.
2. **Project folder**. The Train Tickets folder is mounted at `/sessions/<session>/mnt/Train Tickets/`, with a rotating session name. Discover it via `pwd` (if cwd is inside) or `ls -d /sessions/*/mnt/Train\ Tickets`. Verify it contains `RUNBOOK.md`, `prices.json`, `daily_run.py` — otherwise the mount is missing and the run must fail hard.
3. **Mac-side git is required for commit + push**. The sandbox mount is additive-only so a sandbox-native git commit leaves stale locks. Always commit via `mcp__Control_your_Mac__osascript` (see step 7).

Treat the discovered folder as `$WORK` for the rest of the run.

---

## Sequence

### 1. Read pipeline state

Load `$WORK/prices.json` and classify each entry:

- `tuesdays` with `booked: true` → **skip** (already paid for).
- `tuesdays` with `booked: false` → **scrape today**.
- `not_bookable_yet` → the first entry is today's horizon probe.

### 2. Horizon probe (1 lookup)

Ask the skill to look up Yatton → Paddington for the first date in `not_bookable_yet` (time windows irrelevant).

- If the result returns rail rows → the booking horizon has rolled forward. **Add that date to the scrape list below** — `daily_run.py`'s `add_newly_bookable` will move it from `not_bookable_yet` into `tuesdays` and then validate that `raw_snapshot.json` has rows for it, so if you don't scrape it the run fails.
- If the result comes back `coach_redirect` or empty → still beyond the horizon. That's the expected outcome most nights.

Record the outcome into the snapshot (see step 4).

### 3. Scrape every unbooked Tuesday (blocking)

For each date, call the skill with:

- `origin`: Yatton (`YAT3392gb`)
- `destination`: London Paddington (`PAD3087gb`)
- outward: `<Tuesday>T07:00:00`
- inward: `<Tuesday>T18:00:00`

The skill returns `{outward: [...], inward: [...]}`. Validate inside this runbook (not inside the skill):

- Outward list MUST contain a row with `dep == "07:36"` and a numeric price.
- Inward list MUST contain a row with `dep == "18:30"` and a numeric price.
- Neither row can be null. Sophie's constraints are non-negotiable.

If validation fails, wait 30s, reload the tab, re-extract. Retry up to 3 times per date. On the third failure, record the failure in `raw_snapshot.json[tuesdays][date].error` and move on — don't skip silently.

### 4. Capture 2x-Advance Single premium (non-blocking, per date)

**Non-blocking** — if this fails for any reason, record `null` and continue. Do not retry, do not fail the run. Sophie's booking message is guarded only by step 3.

Why: the scraped 07:36 outward price is a SplitSave fare. This step captures how much extra the 2x-Advance-Single alternative costs, so we can tell whether the £7 SplitSave seesaw tracks the Advance tier or is SplitSave-specific. Validated selectors on 2026-04-24 for 15 Sep (£1.70) and 9 Jun (£13.30).

Per date (≈5–7s, ≈100s total across 20 dates):

1. On the results page from step 3, find the standard-class radio buttons for 07:36 outward and 18:30 return: select `[data-test="train-results-container-OUTWARD"]`, walk its `train-results-departure-time` elements, regex-match `07:36`, walk up to the row's `standard-class-price-radio-btn`. Same for `-INWARD` + `18:30`.
2. If either radio is not `.checked`, click it. Trainline auto-selects the cheapest row by default, NOT the 07:36.
3. Click `[data-test="cjs-button-continue"]`.
4. Wait up to 15s for URL to contain `/book/ticket-options`. Timeout → record null.
5. Parse `document.body.innerText`. Find the `Ticket type` section. Scan for `+£X.XX` lines with the preceding line as the ticket-type name. Expect `SplitSave` `+£0.00`, `2x Single Tickets` `+£X.XX`, `Anytime Return` `+£X.XX`.
6. Record the `2x Single Tickets` delta as `twox_advance_premium` on this date.

### 5. Write `$WORK/raw_snapshot.json`

```json
{
  "probed_at": "<ISO timestamp>",
  "horizon_probe": {
    "probe_date": "YYYY-MM-DD",
    "bookable": false,
    "coach_redirect": true,
    "checked_at": "<ISO timestamp>"
  },
  "tuesdays": [
    {
      "date": "YYYY-MM-DD",
      "outward": [{"dep": "07:36", "arr": "09:34", "price": 86.70}, ...],
      "inward":  [{"dep": "18:30", "arr": "20:27", "price": 27.00}, ...],
      "splitsave": {"available": true, "total": 113.70, "savings_vs_direct": 0},
      "twox_advance_premium": 13.30
    }
  ]
}
```

`twox_advance_premium` is `null` when step 4 failed — that's fine.

### 6. Run the validator + site regen

```
cd $WORK && python3 daily_run.py
```

`daily_run.py` owns all of:

- Validates `raw_snapshot.json` against `prices.json`.
- Updates `prices.json` **only on `ok`**.
- Appends to `fare_history.jsonl` (append-only long-term dataset).
- Computes movement analysis (bulk events, outliers, new historical lows) + patterns.
- Writes `run_status.json` (`ok` or `failed`), and `pending_message.txt` (ok) or `paddy_alert.txt` (failed).
- Regenerates `index.html` + `reminders/*.ics` via `generate_site.main()` on success.

Then read `run_status.json`:

- `ok` → the 06:00 iMessage task will send `pending_message.txt`.
- `failed` → **do NOT send Sophie's iMessage.** Yesterday's prices and message are preserved as-is; `paddy_alert.txt` tells Paddy what went wrong.

### 7. Commit AND push via the Mac

The push is load-bearing — without it, the tracker at <https://sophie-train-tracker.vercel.app> serves yesterday's page. Skip commit+push only if the run was `failed` AND no files changed.

Run via `mcp__Control_your_Mac__osascript`. `paddy_alert.txt` only exists on failed runs — adding it unconditionally makes git fail the whole batch on `ok` runs (observed 2026-04-26: silent no-op despite ~12 modified files because `2>/dev/null` swallowed the fatal). Always-present files go in one `git add`; the alert is added only when present:

```sh
cd "/Users/paddydavies/Documents/Claude/Projects/Train Tickets" \
  && git add prices.json raw_snapshot.json run_status.json horizon_log.jsonl fare_history.jsonl run_log.jsonl pending_message.txt index.html reminders \
  && if [ -f paddy_alert.txt ]; then git add paddy_alert.txt; fi; \
  if ! git diff --cached --quiet; then \
    git commit -m "Daily rail scrape $(date +%Y-%m-%d)" && git push origin main; \
  else \
    echo "no changes to commit"; \
  fi
```

If the push fails (network/auth/conflict), overwrite `paddy_alert.txt` with the git error text so the 06:00 sender pages Paddy instead of firing a stale message.

### 8. Report back

Three-line summary in chat:

- Run status (ok/failed)
- Count of Tuesdays scraped / failed / skipped-booked / premium-captured
- Horizon probe outcome + confirmation that commit+push succeeded (or "no changes")

If anything blocks, stop and ask Paddy — never silently half-complete.

---

## Pricing semantics (validated 2026-04-24)

Kept here for pipeline-layer code readers (and future Claudes interpreting the dataset). The skill's own `SKILL.md` carries the same essentials, in the context of scraping.

- **07:36 outward** `alternative-price` is a **SplitSave** fare — two tickets covering Yatton → Paddington on the same train, refundable until 23:59 day before. NOT a simple Advance Single. The "2x Advance Single" alternative costs £1.70–£13.30 more (see step 4) and has no refunds.
- **18:30 return** is an **Advance Single** at £27.00 in every observation so far. Quota-controlled in principle; has not moved.
- **Booking fee**: Trainline adds a flat **~£2.79** at `/book/ticket-options`. Sophie's true out-the-door cost = scraped total + £2.79. `generate_site.py` constant `BOOKING_FEE_GBP = 2.79`.
- **Pre-selection pitfall**: `/book/results` auto-selects the *cheapest* row, NOT Sophie's 07:36. Any click-through automation (step 4 included) MUST explicitly select the target radios first.

UI labelling lives in `generate_site.py` constants `BOOKING_FEE_GBP`, `SPLITSAVE_LABEL`, `RETURN_LABEL`. If a code change ever drops these labels on the deployed site, something regressed.

---

## Key invariants

- **Sophie's constraints are non-negotiable**: 07:36 out, 18:30 back, both with numeric fares. Any date missing either row is a failure, even if cheaper alternatives exist.
- **Step 3 (primary scrape) blocks. Step 4 (premium capture) does not.** Never let a premium-capture glitch kill Sophie's message.
- **Retries per date: 3 attempts, 30s apart, primary scrape only.** Beyond that it's a real problem worth Paddy's attention.
- **Failures are loud, not silent**: one bad Tuesday aborts Sophie's message. A partial message is worse than no message — she stops trusting it.
- **Yesterday's data is always the safe fallback**: `daily_run.py` never overwrites `prices.json` on a failed run.

---

## Files touched

| File | Writer | Purpose |
| --- | --- | --- |
| `raw_snapshot.json` | Scheduled task (Chrome MCP) | Raw scraped rows per Tuesday + premium |
| `fare_history.jsonl` | `daily_run.py` | Append-only long-term observation log |
| `horizon_log.jsonl` | `daily_run.py` | Append-only daily horizon probe history |
| `prices.json` | `daily_run.py` | Canonical prices — only updated on `ok` |
| `pending_message.txt` | `daily_run.py` → `compose_imessage.py` | Sophie's iMessage body |
| `paddy_alert.txt` | `daily_run.py` | Paddy-only alert on `failed` |
| `run_status.json` | `daily_run.py` | Gate for the 06:00 send step |
| `run_log.jsonl` | `daily_run.py` | Append-only per-run diagnostics |
| `index.html` | `daily_run.py` → `generate_site.py` | Vercel-served tracker page, regenerated on `ok` |
| `reminders/*.ics` | `daily_run.py` → `generate_site.py` | ICS for still-unbookable Tuesdays |

`.env` (contains `GITHUB_TOKEN` — a fine-grained PAT for GitHub API calls from the sandbox, NOT used for the nightly push) is gitignored; never stage or commit it.

---

## Failure modes by priority

1. **Trainline unreachable / bot challenge** — retries exhaust, hard fail.
2. **Layout changed / selectors moved** — validation fails on every Tuesday, hard fail. Selectors are in the skill's `extractor.js` + `SKILL.md`; check the DOM in browser devtools before patching.
3. **Cheap tier gone on one date** — NOT a failure. The 07:36 / 18:30 rows still exist, just at a higher price. Recorded; the status bucket updates.
4. **Horizon shifted backwards unexpectedly** — not a failure, but log it. `horizon_log.jsonl` shows the regression.
5. **2x-Advance premium capture fails** — NOT a failure. Record null, move on.
