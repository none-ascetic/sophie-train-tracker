# Sophie's Train Tracker — mobile-first redesign

**Date**: 2026-04-24
**Owner**: Paddy (review) / Claude (implementation)
**Status**: drafted, awaiting approval to start Phase 1

## Why this exists

The tracker works on desktop but is ~14,000px tall on an iPhone 14 (40+
screen-scrolls). Sophie opens it once a day on her phone to answer one
question: "should I book today?". The hero answers that, but she has to
scroll **past** a 294px moves banner to find the hero, then through 21
card-sized Tuesdays + 6 booked + a 668px patterns panel to see anything
else. Touch targets are below iOS minimums. Several bits of copy drifted
out of sync during the SplitSave/fee rewrite.

See the `/design:design-critique` output in chat for the full findings.

## Principles

1. **Action above the fold on a 844px iPhone viewport** — hero wins the
   first screen. Moves/context second.
2. **One number per card is the "spend" number** — £73.59 all-in, not the
   £43.80 outward leg. Visual weight follows the decision.
3. **Booked dates take one line** — zero action, zero page real estate.
4. **Progressive disclosure** for long reference content (patterns
   explainer, ladders, bulk-event history).
5. **Touch targets ≥ 44px** everywhere Sophie taps.

## Phase 1 — mobile criticals (ship first)

Estimated effort: 1–2 hours. These are independent; bundle into one commit.

| # | Change | File |
| --- | --- | --- |
| 1 | Swap render order: `{hero}` before `{moves_banner}` in `render_html` | `generate_site.py` |
| 2 | Bump `.btn` padding from `8px 14px` → `12px 18px`; min-height `44px` | `generate_site.py` CSS |
| 3 | One `Book return (07:36 / 18:30) — £73.59 all-in` button per card (swap `_render_bookable_card` actions block) | `generate_site.py` |
| 4 | `STATUS_LABELS["STABLE"]` → `"Watch · at median"` (was "at baseline") | `generate_site.py` |
| 5 | Suppress per-card `.save-badge` pill when this Tuesday is part of a bulk event. Only render pill for outliers + new lows. | `generate_site.py` — pass `bulk_event_dates: set` into `_arrow_badge` |

Acceptance: mobile doc height drops below 12,000px; first-screen shows
hero + first 1–2 cards; all tap targets ≥ 44px; "baseline" word gone.

## Phase 2 — card redesign (second pass)

Estimated effort: 2–3 hours. More visually impactful, slightly riskier.

| # | Change | File |
| --- | --- | --- |
| 6 | Swap visual weight: `£73.59 all-in` 30px headline, `£43.80 (07:36 out)` as small supporting line | `generate_site.py` card markup + CSS |
| 7 | Collapse booked cards to one line: `Tue 28 Apr · 1 week out · paid £94.69 · 07:06 / 19:30` | New `_render_booked_line(t)` helper |
| 8 | Tint card background green for `BOOK_TODAY` dates; neutral for `BOOK_SOON`; faint grey for `STABLE` | CSS only |

Acceptance: Sophie sees which cards are the deal at a glance. Booked
section recovers ~2,000px of mobile scroll. No behaviour changes.

## Phase 3 — progressive disclosure (third pass)

Estimated effort: 1 hour.

| # | Change | File |
| --- | --- | --- |
| 9 | Wrap patterns panel in `<details>` (closed by default on mobile via CSS `@media`) | `generate_site.py` + CSS |
| 10 | Move hero's SplitSave+fee sub-line into the patterns explainer so the hero is one sentence | `generate_site.py` |
| 11 | Collapse moves banner on mobile via `<details>` open-on-load (first time) + closed on repeat visits (localStorage flag) | `generate_site.py` — small JS block |

Acceptance: first-screen mobile experience is hero + 1 card + section
title. Everything else is one tap away.

## Phase 4 — stretch (optional, after Paddy reviews Phases 1–3)

These aren't strictly mobile fixes but were flagged by the basket
validation:

- **Scrape the 2x-Advance-Single price** as a secondary signal, so we can
  tell whether the summer £7 seesaw is SplitSave-specific or a general
  Advance-tier event. Requires the scraper to step to `/book/ticket-options`
  and read the `+£X.XX` ticket-type delta. Adds ~3s per date to the nightly
  scrape.
- **Weekly basket-validation spot-check**: every Monday the scheduled task
  picks one unbooked Tuesday, clicks through to the basket, confirms the
  all-in total matches `scraped_total + BOOKING_FEE_GBP`. Write the result
  to `validation_log.jsonl` and fail loud if it drifts by >£1.
- **Booking-fee tracking**: if we see the `+£2.79` fee change (e.g., post-
  Brexit payment-processing rules), capture and update the constant.

## Explicit non-goals

- Not changing `daily_run.py` pipeline logic — it's correct.
- Not changing `fare_history.jsonl` schema — it's correct.
- Not changing the scrape URL or selectors — validated today.
- Not touching `compose_imessage.py` — Sophie's iMessage can lag behind
  the web redesign; messaging logic stays until Phase 2 lands.

## Rollout order

1. Ship Phase 1 as a single commit to `main`. Vercel redeploys within
   a minute. Sophie sees changes on her next morning visit.
2. Let it sit for a day so the nightly `02:00` pipeline regenerates
   through the new code end-to-end.
3. Ship Phase 2 as a single commit the next day.
4. Phase 3 the day after.
5. Phase 4 only if Paddy greenlights after seeing the tracker on mobile
   post-Phases 1–3.

## Risks

- **Suppressing per-card pills on bulk-event dates** means if the banner
  isn't read, Sophie might miss a price drop on a specific card she was
  watching. Mitigation: the status bucket (`BOOK_TODAY` etc.) still
  carries the urgency signal at card level, and the hero + banner call
  out the bulk event at page level.
- **Collapsing booked cards** removes the "nice to glance at" paid
  tickets list. Mitigation: keep the times + price on the one-liner so
  Sophie can still confirm at a glance.
- **Progressive disclosure on moves banner** means some Sophies might
  never open it. Mitigation: default open on the day a bulk event
  fires (detected via `last_run.movements.any_movement`).

## Open questions for Paddy

1. Phase 1 as proposed ok, or any change to the one-button-per-card
   wording? ("Book return — £73.59 all-in" vs. "Book tickets — £73.59"
   vs. other?)
2. Phase 4 — go or no-go on scraping the 2x-Advance price? It's the
   only way to answer "is the £7 seesaw SplitSave-specific?".
3. Anything off-limits in Phases 2–3?
