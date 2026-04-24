# Proposed update: `trainline-lookup` skill → 1.2.0

**Status**: drafted, NOT applied anywhere yet.
**Reason to apply**: the basket validation on 2026-04-24 revealed the scraped `alternative-price` is often a SplitSave combination (not an Advance Single), plus a flat ~£2.79 booking fee at checkout, plus a pre-selection pitfall. Callers need to know this to present prices honestly.

## Where the source lives

The skill appears in two places on this machine and they are currently diverged — Paddy needs to decide which is canonical before applying:

1. `/Users/paddydavies/.claude/skills/trainline-lookup/SKILL.md` — **1.0.0** (22 Apr 2026, `.claude` folder).
2. `/var/folders/c7/r6yr3c2d0pdgs3rcwly1nrt40000gn/T/claude-hostloop-plugins/63f85b9bb15c4023/skills/trainline-lookup/SKILL.md` — **1.1.0** (24 Apr 2026, cowork plugin cache, read-only from the sandbox).

The plugin cache (#2) has the skeleton-guard update that I wrote today but the `.claude` folder (#1) does not. Fix the divergence first, then apply the 1.2.0 changes below.

## Changes in 1.2.0 (pure documentation — no code changes)

### 1. New section: `## Pricing semantics — what the scraped price actually is`

Insert immediately before the `## Version notes` section. Full copy:

```markdown
## Pricing semantics — what the scraped `price` actually is

This section was added 2026-04-24 after a basket-validation exercise (clicked through to `/book/ticket-options` for two dates and read the real totals). Callers who present these prices to users **must** be aware:

### The `price` is not always an "Advance Single"

`[data-test="alternative-price"]` is Trainline's "cheapest bookable option for this row" — which depending on the row can be:

- An **Advance Single** (one ticket, specified train, no refunds).
- A **SplitSave** (two tickets that together cover the journey, usually same train no-change, refundable until 23:59 day before).
- An **Off-Peak / Anytime Single** where those fares are regulated and happen to be cheapest.

You can tell the row has *a* cheapest-Advance-Single offer at that price when the row also has `[data-test="cheapest-price-label"]` with the same £ value. When only `alternative-price` is present (no matching cheapest-label), the price is typically a SplitSave combination — confirmed on the `/book/ticket-options` page as `SplitSave · +£0.00 · Multiple tickets, stay on same train`.

**Implication for callers**: if your UI says "Advance Single" alongside the scraped price, you're lying some of the time. Say "cheapest available fare" or click through to `/book/ticket-options` to confirm the product type.

### Trainline adds a flat ~£2.79 booking fee at checkout

Not visible on the results page. Appears on `/book/ticket-options` as the difference between the Standard headline and the "Total" number. Observed £2.79 on both validation dates (Sophie's 15 Sep £70.80 → £73.59; 9 Jun £113.70 → £116.49). Callers showing "all-in" totals should add this.

### Pre-selection pitfall

When Trainline loads `/book/results` with `selectedOutward=...` / `selectedInward=...` in the URL, it accepts those IDs. **But** if you don't pass them (or they're stale), Trainline auto-selects the *cheapest* standard-class row on each leg — which might not be the user's target train. Example: Sophie needs the 07:36 but at 6 months out the 08:23 is cheaper, so the default selection arrives too late (10:45). Downstream impact: if anything after this skill acts on "the selected train", check the radios explicitly rather than trusting pre-selection.

### Validating a scraped price against the basket

When you want to be sure a price is real and bookable (not a mid-hydration artifact or a split-ticket the user doesn't want):

1. Navigate to the results page (this skill's URL format).
2. Click the standard-class radio for the target departure time on both legs.
3. Click `[data-test="cjs-button-continue"]`.
4. On `/book/ticket-options`, read the `h3` with text `Total`, then the `span` below it.
5. That's the true out-the-door cost (ticket + fee).
```

### 2. Prepend to version notes

```markdown
- **1.2.0** (24 Apr 2026): Pricing-semantics section added after the basket validation revealed the scraped `alternative-price` is often a SplitSave combination, not an Advance Single. Also documents the flat ~£2.79 booking fee and the pre-selection pitfall that auto-selects the cheapest row rather than the user's target. No code changes — pure documentation; the extractor still returns honest prices, just now with the meta-data callers need to present them correctly.
```

## What this does NOT change

- `extractor.js` — unchanged, still 1.1.0's skeleton-guarded implementation.
- `stations.md` — unchanged.
- The scraped output shape — unchanged.

Pure documentation update so future callers (or future-me) don't re-make the "this is an Advance Single" mistake.
