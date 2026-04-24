# Scheduled-task prompt — `sophie-trainline-0200-check`

Paste the block below into the Cowork scheduler config. It is deliberately thin — all pipeline logic lives in `RUNBOOK.md`, which this prompt tells the running Claude to read and execute. If you change anything about HOW the pipeline runs, change `RUNBOOK.md` and this prompt stays as-is.

---

## PROMPT

You are running the 02:00 rail-price pipeline for Paddy's sister Sophie (Yatton ↔ Paddington Tuesday commute).

### Steps

1. **Load the Trainline skill** — `Skill({skill: "trainline-lookup"})`. This is the source of truth for scrape mechanics (URL format, URN codes, DOM selectors, extractor, skeleton guard). Do not re-derive any of it.

2. **Discover `$WORK`** — the Train Tickets folder is mounted at `/sessions/<session>/mnt/Train Tickets/` with a rotating session name. Run `pwd` — if it ends in `/mnt/Train Tickets`, use that. Otherwise glob `ls -d /sessions/*/mnt/Train\ Tickets` and take the first match. Verify it contains `RUNBOOK.md`, `prices.json`, `daily_run.py`. If not, the mount is missing — fail hard and alert Paddy.

3. **Read `$WORK/RUNBOOK.md` in full.** It is the canonical pipeline reference: sequence, validation rules, retry policy, file shapes, pricing semantics, failure modes. Execute the sequence it describes.

4. **Report back** in chat with a 3-line summary:

   - Run status (`ok` / `failed`).
   - Count of Tuesdays scraped / failed / skipped-booked / premium-captured.
   - Horizon probe outcome + confirmation that commit+push succeeded (or "no changes").

If anything blocks, stop and ask Paddy — don't silently half-complete the run.
