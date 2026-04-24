# Scheduled-task prompt — `sophie-trainline-0200-check`

Paste the block below into the scheduled-task config. No hardcoded session
name (the Cowork session rotates every run), no workaround inline dumps —
the Trainline scraping logic is owned by the installed `trainline-lookup`
skill and loaded via the Skill tool.

---

## PROMPT

You are running the 02:00 rail-price pipeline for Paddy's sister Sophie
(Yatton ↔ Paddington Tuesday commute).

### 1. Load the Trainline skill

`Skill({skill: "trainline-lookup"})` — this is the source of truth for
the Trainline URL format, URN station codes, DOM selectors, and the
date-guarded extractor. Do not re-derive any of it.

### 2. Find the project folder

The Train Tickets folder is mounted at `/sessions/<session>/mnt/Train Tickets/`
but the session name rotates every run. Discover it:

- `pwd` in bash — if it already ends in `/mnt/Train Tickets`, use that.
- Otherwise glob: `ls -d /sessions/*/mnt/Train\ Tickets` and take the
  first match.
- Verify it contains `RUNBOOK.md`, `prices.json`, and `daily_run.py`.
  If not, fail hard and alert Paddy — the mount is missing.

Treat that path as `$WORK` for the rest of the run.

### 3. Read the runbook

`Read $WORK/RUNBOOK.md` in full. It describes the pipeline sequence,
file shapes, validation rules, and failure-handling policy. The scrape
mechanics live in the `trainline-lookup` skill — the runbook is the
pipeline layer on top.

> **Pricing semantics (validated 2026-04-24 via basket clickthrough)**.
> The price this skill scrapes on `[data-test="alternative-price"]` for
> the 07:36 outward is consistently a **SplitSave** fare (two tickets,
> same train, refundable day-before) — NOT a single Advance ticket. The
> 18:30 return is an **Advance Single** at a steady £27. Trainline adds
> a flat **~£2.79 booking fee** at `/book/ticket-options` on top of the
> ticket price. The numbers are correct — just know what the user will
> actually receive and pay when they book. `generate_site.py` already
> labels this honestly in the tracker UI and surfaces the all-in total;
> don't revert those labels if you're re-running after a code change.
> See `trainline-lookup` skill v1.2.0 notes for the full write-up.

### 4. Load pipeline state

Read `$WORK/prices.json`:

- `tuesdays` with `booked: true` → SKIP (already paid for).
- `tuesdays` with `booked: false` → scrape today.
- `not_bookable_yet` → the first entry is today's horizon probe.

### 5. Horizon probe (1 lookup)

Take the first date from `not_bookable_yet`. Ask the skill to look up
Yatton → Paddington for that date (time windows irrelevant for the
probe). If the result returns rows, the horizon has rolled forward —
log it. If the result comes back `coach_redirect` or empty, the date
is still beyond horizon — that's the expected outcome most nights.
Record the outcome for the snapshot (see step 7).

### 6. Scrape every unbooked Tuesday

For each unbooked Tuesday, call the skill with:

- origin: Yatton (`YAT3392gb`)
- destination: London Paddington (`PAD3087gb`)
- outward date: `<Tuesday>T07:00:00`
- inward date: `<Tuesday>T18:00:00`

The skill returns `{outward: [...], inward: [...]}` with every visible
row as `{dep, arr, price}`.

**Validation** (done in this task, not in the skill):

- Outward list MUST contain a row with `dep === "07:36"` and a numeric
  price.
- Inward list MUST contain a row with `dep === "18:30"` and a numeric
  price.
- Neither row can be null. Sophie's constraints are non-negotiable.

If validation fails, retry up to 3 times with a 30s gap. On the 3rd
failure, mark the date as failed in the snapshot and move on — don't
skip silently.

### 6b. Capture 2x-Advance Single premium (Phase 4, added 2026-04-24)

After a Tuesday passes validation, also capture `twox_advance_premium`
— how much extra Sophie would pay to swap the default SplitSave outward
for a 2x Advance Single ticket. This tells us whether the £7 seesaw on
the SplitSave outward is SplitSave-specific or tracks the Advance tier
as well. Optional: if a capture fails, leave the field null — don't
block the run. The primary SplitSave scrape is what guards Sophie's
booking message.

Steps per date (~5–7s per Tuesday, ~100s total across 20 dates):

1. On the results page (same URL as step 6), find the standard-class
   radio buttons for 07:36 outward and 18:30 return: select
   `[data-test="train-results-container-OUTWARD"]`, walk its
   `train-results-departure-time` elements, regex-match `07:36`, walk
   up to the row's `standard-class-price-radio-btn`. Same for
   `-INWARD` + `18:30`.
2. If either radio is not `.checked`, click it. Trainline auto-selects
   the *cheapest* train by default — which is NOT the 07:36 at 6-month
   range (it's the 08:23 arriving 10:45, too late).
3. Click `[data-test="cjs-button-continue"]`.
4. Wait for URL to contain `/book/ticket-options` (up to 15s). If the
   wait times out, record null premium and move on.
5. Parse `document.body.innerText` — find the `Ticket type` section,
   scan for lines matching `^\+£([\d.]+)$` with the preceding line as
   the ticket-type name. Expect: `SplitSave` `+£0.00`, `2x Single
   Tickets` `+£X.XX`, `Anytime Return` `+£X.XX`.
6. Record the `2x Single Tickets` delta as the date's
   `twox_advance_premium` (float). If not found, record null.
7. Return to results: `window.history.back()`, or simply navigate to
   the next date's URL for the following iteration.

Validated working 2026-04-24 for 15 Sep (£1.70) and 9 Jun (£13.30).

### 7. Write `$WORK/raw_snapshot.json`

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
      "twox_advance_premium": 13.30  // null if capture failed (non-blocking)
    }
  ]
}
```

### 8. Run the validator

```
cd $WORK && python3 daily_run.py
```

It validates `raw_snapshot.json` against `prices.json`, updates
`prices.json` only on `ok`, writes `run_status.json`,
`pending_message.txt` (on ok) or `paddy_alert.txt` (on failed), and on
success also calls `generate_site.main()` to rewrite `index.html` +
`reminders/*.ics` from the fresh `prices.json`. No separate site-
regen step is needed — but both those files MUST be in the git add
list below or the refreshed HTML never reaches Vercel.

### 9. Commit via Mac (not sandbox)

The Cowork sandbox mount is additive-only: it can create files inside
`.git/` but cannot unlink them, so a sandbox-native `git commit` leaves
stale `.git/index.lock`, `HEAD.lock`, and `tmp_obj_*` files that block
every subsequent run. Use the Mac, where `rm` works normally. Commit
through the Mac's git at `/Users/paddydavies/Documents/Claude/Projects/Train Tickets`
via `mcp__Control_your_Mac__osascript`:

```
cd "/Users/paddydavies/Documents/Claude/Projects/Train Tickets" \
  && git add prices.json raw_snapshot.json run_status.json horizon_log.jsonl pending_message.txt paddy_alert.txt index.html reminders/ 2>/dev/null \
  && git diff --cached --quiet || git commit -m "Daily rail scrape $(date +%Y-%m-%d)" \
  && git push
```

`index.html` + `reminders/` MUST be in the add list — otherwise the
regenerated site stays on disk but the remote (and therefore Vercel)
never sees it. Skip commit+push entirely only if the run was `failed`
AND nothing meaningful changed.

> **Note on `GITHUB_TOKEN`**: a fine-grained PAT lives in `$WORK/.env`.
> It is NOT used for the nightly push. It's available for GitHub API
> calls from the sandbox where unlinking isn't involved (issues,
> workflow dispatches, content API). `.env` is gitignored.

### 10. Report back

Reply in chat with a 3-line summary:

- Run status (ok/failed)
- Count of Tuesdays scraped / failed / skipped-booked
- Horizon probe outcome (bookable yes/no)

If anything blocks, stop and ask Paddy — don't silently half-complete
the run.
