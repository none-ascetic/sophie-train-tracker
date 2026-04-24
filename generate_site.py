#!/usr/bin/env python3
"""Regenerate index.html + index.artifact.html + reminders/*.ics from prices.json.

Built minimal-but-complete: the HTML shell (header, CSS, footer) lives inline here
so we don't depend on _template.html as anything other than a historical design
reference. Cards are emitted from prices.json (tuesdays[] and not_bookable_yet[]).

Usage: python3 generate_site.py
"""
import json
import html
from datetime import datetime, date, time, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
PRICES = ROOT / "prices.json"
INDEX = ROOT / "index.html"
ARTIFACT = ROOT / "index.artifact.html"
REMINDERS_DIR = ROOT / "reminders"
TRACKER_URL = "https://sophie-train-tracker.vercel.app"

# Trainline opens booking ~179 days ahead and the window rolls forward one day at a time.
# See RUNBOOK.md "Booking horizon" and horizon_log.jsonl for the live probe. Do NOT change
# this to 12 weeks / 84 days — that was a miscount that showed release dates 3 months late.
HORIZON_DAYS = 179

# Trainline deep-link station hashes (copied from existing template)
YAT = "1551a38ad87e8710d21b25403ae0a3e6"
PAD = "1f06fc66ccd7ea92ae4b0a550e4ddfd1"
DOB = "1995-01-01"

# Booking fee Trainline adds at checkout — flat £2.79 observed on both
# validation dates (Tue 15 Sep total £70.80 → £73.59; Tue 9 Jun total
# £113.70 → £116.49). Displayed so Sophie sees the real out-the-door cost,
# not the ticket-only number. If this changes we'll spot it in the basket
# validation runs and bump it here.
BOOKING_FEE_GBP = 2.79

# The 07:36 outward is sold as SplitSave on every date we've validated.
# Two tickets, stay on the SAME train (no changes), refundable until 23:59
# the day before travel. The "2x Advance Single" alternative is always
# £1.70–£13.30 more expensive AND has no refunds — so SplitSave is the
# correct default. Sophie does receive two tickets in her booking email.
SPLITSAVE_LABEL = "SplitSave · same train, 2 tickets · refundable day-before"

# The 18:30 return is consistently labelled "Advance Single" at £27 across
# every observation. Specified train only, no refunds, but the £27 price
# has been absolutely stable so far.
RETURN_LABEL = "Advance Single · specified train only"

STATUS_LABELS = {
    "URGENT": ("Cheap tier gone", "urgent"),
    "BOOK_TODAY": ("Book today", "today"),
    "BOOK_SOON": ("Book soon", "soon"),
    "STABLE": ("Watch · at median", "stable"),
    "UNKNOWN": ("Awaiting data", "stable"),
    "BOOKED": ("Already booked", "booked"),
}

SECTION_ORDER = [
    ("URGENT", "Urgent · cheap tier gone"),
    ("BOOK_TODAY", "Book this week · last cheap tier"),
    ("BOOK_SOON", "Book soon · within 1–2 weeks"),
    ("STABLE", "At median · sit tight, book in the next fortnight"),
    ("UNKNOWN", "Awaiting data"),
    ("BOOKED", "Already booked · paid tickets"),
]


# ---------- formatting helpers ----------

def _fmt_gbp(x):
    if x is None:
        return "—"
    return f"£{x:.0f}" if x == int(x) else f"£{x:.2f}"


def _fmt_gbp2(x):
    """Always two decimals — used for all-in totals where pennies matter."""
    if x is None:
        return "—"
    return f"£{x:.2f}"


def _all_in(ticket_total: float | None) -> float | None:
    """Ticket price + Trainline booking fee = what Sophie actually pays."""
    if ticket_total is None:
        return None
    return round(ticket_total + BOOKING_FEE_GBP, 2)


def _fmt_date_short(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return d.strftime("%a %-d %b")


def _fmt_date_long(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return d.strftime("%-d %B %Y")


def _weeks_out(travel_iso: str) -> int:
    td = datetime.strptime(travel_iso, "%Y-%m-%d").date() - date.today()
    return max(0, (td.days + 3) // 7)


def _trainline_url(iso_date: str, direction: str, hhmm: str) -> str:
    """Return the /dpi deep link, requesting 6 min before the actual train time."""
    h, m = [int(x) for x in hhmm.split(":")]
    # Outbound: 30 min pre-roll; return: 6 min pre-roll (matches template precedent)
    pre = 30 if direction == "out" else 6
    adj = (h * 60 + m) - pre
    if adj < 0:
        adj = 0
    ah, am = divmod(adj, 60)
    o, d = (YAT, PAD) if direction == "out" else (PAD, YAT)
    return (
        f"https://www.thetrainline.com/dpi?locale=en&origin={o}&destination={d}"
        f"&outboundType=departAfter"
        f"&outboundTime={iso_date}T{ah:02d}%3A{am:02d}%3A00"
        f"&affiliateCode=tlseo&currency=GBP"
        f"&passengers%5B0%5D%5Bdob%5D={DOB}&journeyType=single"
    )


def _trainline_return_url(iso_date: str) -> str:
    """Return-journey deep link for Sophie's fixed 07:36 out + 18:30 back.
    Lands her on the /book/results page for that Tuesday with the journey
    type = return and both dates pre-filled. She still picks the 07:36 /
    18:30 rows manually — Trainline's selected-outward URL params are
    ephemeral so we can't pre-click them from a static link. This matches
    what the scraper opens, which is what was validated end-to-end on
    2026-04-24. Single booking flow — gets her one SplitSave+Advance
    booking at the right total, not two separate one-way tickets."""
    return (
        "https://www.thetrainline.com/book/results"
        "?journeySearchType=return"
        "&origin=urn%3Atrainline%3Ageneric%3Aloc%3AYAT3392gb"
        "&destination=urn%3Atrainline%3Ageneric%3Aloc%3APAD3087gb"
        f"&outwardDate={iso_date}T07%3A00%3A00"
        "&outwardDateType=departAfter"
        f"&inwardDate={iso_date}T18%3A00%3A00"
        "&inwardDateType=departAfter"
        "&selectedTab=train&splitSave=true&lang=en"
        "&transportModes%5B%5D=mixed"
    )


# ---------- card rendering ----------

def _arrow_badge(
    change: dict,
    movement_context: dict | None = None,
    *,
    suppress: bool = False,
) -> str:
    """Pill showing day-over-day move. When we know WHICH leg moved
    (via movement_context), we say so — much more useful to Sophie than
    a bare "vs yesterday" that hides whether the outward or return
    changed. movement_context is this Tuesday's row from
    last_run.movements.per_tuesday (dict or None).

    Priority order for the delta:
      1. movement_context.d_total — sourced from fare_history, immune to
         same-day re-runs overwriting the in-memory `change_vs_yesterday`.
      2. change.cheapest_any — the legacy field, left as a fallback for
         historical data read before movements existed.

    `suppress=True` returns an empty string. Used when a Tuesday's move
    is already carried by the Today's-Moves banner (bulk event membership);
    the per-card pill would be 13 identical repeats of the banner's
    headline, so we drop it to reduce visual noise — unless this specific
    card *also* has a new-low or outlier story, in which case the caller
    should pass suppress=False."""
    if suppress:
        return ""
    delta = None
    if movement_context and isinstance(movement_context.get("d_total"), (int, float)):
        delta = movement_context["d_total"]
    elif change and isinstance(change.get("cheapest_any"), (int, float)):
        delta = change["cheapest_any"]
    if delta is None or abs(delta) < 0.01:
        return ""
    # Decide what leg to tag. Prefer movement_context (richer) over the bare
    # total-only delta.
    leg_note = ""
    if movement_context:
        d_out = movement_context.get("d_out")
        d_back = movement_context.get("d_back")
        out_moved = isinstance(d_out, (int, float)) and abs(d_out) >= 0.01
        back_moved = isinstance(d_back, (int, float)) and abs(d_back) >= 0.01
        if out_moved and not back_moved:
            leg_note = " on 07:36 out"
        elif back_moved and not out_moved:
            leg_note = " on 18:30 back"
        elif out_moved and back_moved:
            leg_note = " on both legs"
    sign = "↓" if delta < 0 else "↑"
    direction_class = "save-badge" if delta < 0 else "save-badge up"
    style = "" if delta < 0 else ' style="background:#b42318"'
    return (
        f' <span class="{direction_class}"{style}>'
        f'{sign} {_fmt_gbp(abs(delta))}{leg_note} vs yesterday</span>'
    )


def _new_low_badge(new_lows_by_date: dict, travel_date: str) -> str:
    """Red-orange 'NEW LOW' pill when today's total beats every prior
    observation of this Tuesday. Driven by last_run.movements.new_lows,
    not heuristics — so we don't false-positive on first-ever observations."""
    info = new_lows_by_date.get(travel_date)
    if not info:
        return ""
    return (
        f' <span class="new-low-badge" title="Beats prior low of '
        f'{_fmt_gbp(info.get("prior_low"))} by {_fmt_gbp(info.get("beats_by"))} '
        f'across {info.get("observations")} prior checks">🎯 NEW LOW</span>'
    )


def _alts_block(cur: dict) -> str:
    out_alts = cur.get("out_alternatives") or []
    back_alts = cur.get("back_alternatives") or []
    primary_back_fare = (cur.get("back") or {}).get("fare")
    # Only show the block if there's something interesting (cheaper back alt, or any alt)
    items = []
    for a in out_alts[:2]:
        t, f, ch = a.get("time"), a.get("fare"), a.get("changes") or 0
        label = "direct" if ch == 0 else f"{ch} change{'s' if ch > 1 else ''}"
        items.append(f"<li>Out <strong>{html.escape(t)}</strong> — {_fmt_gbp(f)} · {label}</li>")
    for a in back_alts[:3]:
        t, f, ch = a.get("time"), a.get("fare"), a.get("changes") or 0
        label = "direct" if ch == 0 else f"{ch} change{'s' if ch > 1 else ''}"
        badge = ""
        if primary_back_fare is not None and f is not None and f < primary_back_fare - 0.5:
            badge = f' <span class="save-badge">saves {_fmt_gbp(primary_back_fare - f)}</span>'
        items.append(
            f"<li>Back <strong>{html.escape(t)}</strong> — {_fmt_gbp(f)} · {label}{badge}</li>"
        )
    if not items:
        return ""
    return (
        '<div class="alts">'
        '<div class="alts-head">Nearby options</div>'
        f'<ul>{"".join(items)}</ul>'
        '</div>'
    )


def _all_in_row(cur: dict, route_median: float | None) -> str:
    """Total row — ticket price + flat Trainline fee = real out-the-door cost.
    Compared to the route median (what we typically see on this line) rather
    than the old £127 baseline, which predates the Advance-fare dataset and
    doesn't reflect anything Sophie sees on real bookings."""
    total = cur.get("cheapest_any_total")
    if total is None:
        return '<div class="totals"><span>No total yet</span></div>'
    all_in = _all_in(total)
    core = (
        f'<span>Tickets <strong>{_fmt_gbp(total)}</strong> '
        f'+ ~{_fmt_gbp2(BOOKING_FEE_GBP)} fee '
        f'= <strong>{_fmt_gbp2(all_in)} all-in</strong></span>'
    )
    # Comparison to median ONLY when we actually have route_median from
    # fare_history (i.e., not the first-ever run). Gracefully degrade.
    if isinstance(route_median, (int, float)):
        if total > route_median + 0.5:
            flag = f'<span class="over">{_fmt_gbp(total - route_median)} over route median</span>'
        elif total < route_median - 0.5:
            flag = f'<span class="base">{_fmt_gbp(route_median - total)} under route median</span>'
        else:
            flag = '<span class="base">at route median</span>'
    else:
        flag = '<span class="base">median forming</span>'
    return f'<div class="totals">{core}{flag}</div>'


def _outward_hero(cur: dict) -> str:
    """Big primary block for the 07:36 — the ONLY leg that moves day-to-day
    on this route, so it earns the visual weight. Labels the price as
    SplitSave because that's what Trainline delivers at checkout."""
    out = cur.get("out") or {}
    fare = out.get("fare")
    dep = out.get("time") or "07:36"
    arr = out.get("arrival")
    arr_bit = f" · arrives <strong>{html.escape(arr)}</strong>" if arr else ""
    fare_str = _fmt_gbp(fare) if fare is not None else "—"
    return (
        '<div class="outward-hero">'
        f'<div class="outward-label">OUT · <strong>{html.escape(dep)}</strong>{arr_bit}</div>'
        f'<div class="outward-fare">{fare_str}</div>'
        f'<div class="outward-meta">{html.escape(SPLITSAVE_LABEL)}</div>'
        '</div>'
    )


def _return_footnote(cur: dict) -> str:
    """Compact one-line footnote for the 18:30. It's an Advance Single at
    £27 every observation we've made; no point giving it equal card real
    estate when Sophie's decision is always 'yep, £27, book it'. If it
    ever does move, the moves banner will flag it and the card-level pill
    will light up."""
    back = cur.get("back") or {}
    fare = back.get("fare")
    dep = back.get("time") or "18:30"
    arr = back.get("arrival")
    arr_bit = f" → {html.escape(arr)}" if arr else ""
    fare_str = _fmt_gbp(fare) if fare is not None else "—"
    return (
        '<div class="return-foot">'
        f'+ <strong>{html.escape(dep)}</strong>{arr_bit} return '
        f'<strong>{fare_str}</strong> · {html.escape(RETURN_LABEL)}'
        '</div>'
    )


def _render_bookable_card(
    t: dict,
    *,
    movement_ctx: dict | None = None,
    new_lows_by_date: dict | None = None,
    route_median: float | None = None,
    suppress_pill_dates: set | None = None,
) -> str:
    """Rebuilt 2026-04-24 after basket validation. Outward-primary layout —
    the 07:36 is the only leg that moves on this route, so it gets the
    visual weight. 18:30 demoted to a one-line footnote. All-in total
    surfaces Trainline's £2.79 booking fee so Sophie sees the real
    out-the-door cost before she taps Book. Price comparison anchored to
    route median (learned from fare_history) instead of the old £127
    baseline — that number pre-dated the Advance-fare dataset."""
    cur = t.get("current") or {}
    status = t.get("status", "UNKNOWN")
    status_label, status_class = STATUS_LABELS.get(status, STATUS_LABELS["UNKNOWN"])
    wk = _weeks_out(t["date"])
    out_leg = cur.get("out")
    back_leg = cur.get("back")
    out_time = (out_leg or {}).get("time") or "07:36"
    back_time = (back_leg or {}).get("time") or "18:30"
    note = t.get("note") or ""
    # Suppress the card-level pill when this Tuesday is part of a bulk
    # event (same move applied to many dates at once) UNLESS it also has
    # a new-low story of its own — a new-low is date-specific news the
    # banner doesn't cover.
    has_new_low = t["date"] in (new_lows_by_date or {})
    suppress = (
        suppress_pill_dates is not None
        and t["date"] in suppress_pill_dates
        and not has_new_low
    )
    change_badge = _arrow_badge(
        t.get("change_vs_yesterday"), movement_ctx, suppress=suppress
    )
    new_low_badge = _new_low_badge(new_lows_by_date or {}, t["date"])
    is_booked = bool(t.get("booked"))

    # When the scrape didn't capture leg-level data, render one honest line
    # rather than two empty slots — Sophie shouldn't have to decode empty UI.
    if out_leg is None and back_leg is None:
        total = cur.get("cheapest_any_total")
        if total is not None:
            body = (
                '<div class="legs-summary">'
                f'Cheapest return: <strong>{_fmt_gbp(total)}</strong> · leg times refresh next run'
                '</div>'
            )
        else:
            body = (
                '<div class="legs-summary">Price not captured this run — re-checking tomorrow</div>'
            )
    else:
        body = _outward_hero(cur) + _return_footnote(cur)

    if is_booked:
        actions_block = (
            '<div class="note" style="color:#8a8a8a; font-style:italic;">'
            'Tickets already paid for — no action needed.'
            '</div>'
        )
    else:
        # Single primary button — matches SplitSave's one-booking reality
        # (Paddy's call, 2026-04-24). All-in price (ticket + £2.79 fee) is
        # what Sophie will actually pay; putting that number on the button
        # makes the tap decision explicit.
        all_in_total = _all_in(cur.get("cheapest_any_total"))
        price_suffix = f" — {_fmt_gbp2(all_in_total)}" if all_in_total is not None else ""
        actions_block = (
            '<div class="actions">'
            f'<a class="btn btn-primary" href="{_trainline_return_url(t["date"])}" target="_blank" rel="noopener">'
            f'Book tickets{price_suffix} →</a>'
            '</div>'
        )

    card_class = "card booked" if is_booked else "card"
    note_block = f'<div class="note">{html.escape(note)}</div>' if note else ""
    return f"""
<div class="{card_class}">
  <div class="card-head">
    <div><span class="date">{_fmt_date_short(t['date'])}</span><span class="weeks-out">{wk} week{'s' if wk != 1 else ''} out</span>{change_badge}{new_low_badge}</div>
    <div class="status {status_class}">{html.escape(status_label)}</div>
  </div>
  {body}
  {_all_in_row(cur, route_median)}
  {note_block}
  {actions_block}
</div>
""".strip()


def _render_pending_card(iso_date: str) -> str:
    # Ticket release is ~179 days before travel date (Trainline booking horizon).
    travel = datetime.strptime(iso_date, "%Y-%m-%d").date()
    release = travel - timedelta(days=HORIZON_DAYS)
    release_txt = release.strftime("%-d %b")
    return f"""
    <div class="pending-card">
      <div class="pending-date">{_fmt_date_short(iso_date)}</div>
      <div class="pending-release">Releases around <strong>{release_txt}</strong></div>
      <a class="pending-btn" href="reminders/{iso_date}.ics" download>Add to Reminders</a>
    </div>
""".rstrip()


# ---------- movements banner + patterns panel ----------

def _render_movements_banner(last_run: dict) -> str:
    """Top-of-page banner that CONSOLIDATES today's moves into a readable
    narrative instead of repeating 14 identical pills down the cards. This
    is the direct fix for "14 dates all -£7 doesn't pass the sniff test" —
    we tell Sophie "one tier step hit these 14 dates" up front.

    Content priority:
      1. Bulk events (most load-bearing — explain in plain English)
      2. New historical lows (actionable — book-now signal)
      3. Outliers (worth a look, distinct from the consensus move)
      4. "No changes" quiet day (reassurance, so she knows the pipeline ran)
    """
    movements = (last_run or {}).get("movements") or {}
    if not movements:
        return ""
    bulk = movements.get("bulk_events") or []
    outliers = movements.get("outliers") or []
    new_lows = movements.get("new_lows") or []
    unchanged = movements.get("unchanged_count") or 0
    if not bulk and not outliers and not new_lows:
        return (
            '<div class="moves moves-quiet">'
            f'Quiet night — no fare moves across {unchanged} tracked Tuesdays.'
            '</div>'
        )

    bits = []
    for ev in bulk:
        direction = "down" if ev["delta"] < 0 else "up"
        arrow = "↓" if ev["delta"] < 0 else "↑"
        # All outward prices we scrape are the SplitSave fare. Label
        # explicitly so Sophie knows it's the split-ticket price that moved
        # (not the 2x Advance Single product, which usually holds steadier).
        leg_label = "07:36 SplitSave outward" if ev["leg"] == "outward" else "18:30 return"
        date_list = ", ".join(_fmt_date_short(d) for d in ev["dates"][:6])
        more = f" +{len(ev['dates']) - 6} more" if len(ev["dates"]) > 6 else ""
        bits.append(
            f'<li class="move-bulk"><strong>{arrow} {_fmt_gbp(abs(ev["delta"]))}</strong> '
            f'on the <strong>{leg_label}</strong> — '
            f'{_fmt_gbp(ev["from"])} → {_fmt_gbp(ev["to"])} '
            f'<span class="move-count">({ev["count"]} dates)</span>. '
            f'<span class="move-dates">{date_list}{more}.</span> '
            f'<span class="move-context">Single Advance-tier '
            f'{"release" if direction == "down" else "withdrawal"} — '
            f'these all moved in lockstep, so it\'s one pricing event, not {ev["count"]} independent signals.</span>'
            f'</li>'
        )
    for nl in new_lows:
        bits.append(
            f'<li class="move-low"><span class="new-low-badge">🎯 NEW LOW</span> '
            f'<strong>{_fmt_date_short(nl["date"])}</strong> — '
            f'{_fmt_gbp(nl["total"])} '
            f'(beats prior low of {_fmt_gbp(nl["prior_low"])} by '
            f'{_fmt_gbp(nl["beats_by"])}, {nl["observations"]} prior checks). '
            f'<span class="move-context">Actionable — consider booking.</span>'
            f'</li>'
        )
    for o in outliers:
        # Describe the move in concrete £ terms the reader can double-check.
        pieces = []
        d_out = o.get("delta_out")
        d_back = o.get("delta_back")
        if isinstance(d_out, (int, float)) and abs(d_out) >= 0.01:
            pieces.append(
                f"07:36 out {_fmt_gbp(o['from_out'])} → {_fmt_gbp(o['to_out'])}"
            )
        if isinstance(d_back, (int, float)) and abs(d_back) >= 0.01:
            pieces.append(
                f"18:30 back {_fmt_gbp(o['from_back'])} → {_fmt_gbp(o['to_back'])}"
            )
        desc = " · ".join(pieces) if pieces else "check leg fares"
        bits.append(
            f'<li class="move-outlier">⚠️ <strong>{_fmt_date_short(o["date"])}</strong> '
            f'moved differently from the pack — {desc}. '
            f'<span class="move-context">Worth a quick look.</span></li>'
        )
    unchanged_line = ""
    if unchanged:
        unchanged_line = (
            f'<div class="moves-quiet-inline">No change on {unchanged} other Tuesdays.</div>'
        )
    return (
        '<div class="moves">'
        '<div class="moves-head">Today\'s moves</div>'
        f'<ul class="moves-list">{"".join(bits)}</ul>'
        f'{unchanged_line}'
        '</div>'
    )


def _render_patterns_panel(patterns: dict) -> str:
    """Bottom-of-page 'what we\\'ve learned about this route' panel. This is
    where the long-term dataset starts paying off — as fare_history fills
    out, the ladders sharpen, the route-low anchors, and the bulk-event
    list gets Sophie to a mental model of when fares move.

    Kept deliberately simple: rules discovered via explicit stats, not ML.
    Empty-safe for the first few runs when history is thin."""
    if not patterns:
        return ""
    obs_total = patterns.get("observations_total") or 0
    if obs_total == 0:
        return (
            '<div class="patterns">'
            '<div class="patterns-head">What we know about this route</div>'
            '<div class="patterns-body">No observations yet — check back after tomorrow\'s run.</div>'
            '</div>'
        )

    rmin = patterns.get("route_min")
    rmed = patterns.get("route_median")
    rmax = patterns.get("route_max")
    ladder_out = patterns.get("fare_ladder_out_07_36") or []
    ladder_back = patterns.get("fare_ladder_back_18_30") or []
    bulk_30d = patterns.get("bulk_events_last_30d") or []
    first_obs = patterns.get("first_observation")

    ladder_items = []
    if ladder_out:
        rungs = " / ".join(_fmt_gbp(f) for f in ladder_out)
        ladder_items.append(
            f'<li><strong>07:36 outward tier ladder:</strong> {rungs}. '
            f'Fares cycle between these rungs as Advance inventory fills/releases — '
            f'when you see the lowest rung, book.</li>'
        )
    if ladder_back:
        rungs = " / ".join(_fmt_gbp(f) for f in ladder_back)
        ladder_items.append(
            f'<li><strong>18:30 return tier ladder:</strong> {rungs}.</li>'
        )

    range_line = ""
    if all(x is not None for x in (rmin, rmed, rmax)):
        range_line = (
            f'<li><strong>Total return range:</strong> {_fmt_gbp(rmin)} cheapest ever seen · '
            f'{_fmt_gbp(rmed)} median · {_fmt_gbp(rmax)} highest. '
            f'If today\'s number is near the median, sit tight; near the min, book.</li>'
        )

    events_line = ""
    if bulk_30d:
        last = bulk_30d[-1]
        direction = "drop" if last["delta"] < 0 else "rise"
        events_line = (
            f'<li><strong>Recent bulk events (30d):</strong> '
            f'{len(bulk_30d)} detected. Most recent: '
            f'{last["observed_on"]} — {direction} of '
            f'{_fmt_gbp(abs(last["delta"]))} on the '
            f'{"07:36 outward" if last["leg"] == "outward" else "18:30 return"} '
            f'affecting {last["affected_count"]} dates.</li>'
        )

    meta_line = (
        f'<li class="patterns-meta">Dataset: {obs_total} observations across '
        f'{len(patterns.get("all_time_low_by_tuesday") or {})} Tuesdays, '
        f'since {first_obs or "?"}.</li>'
    )

    items = range_line + "".join(ladder_items) + events_line + meta_line

    # Explainer sits ABOVE the stats so anyone landing on the page cold
    # understands what "SplitSave", "all-in" and "route median" actually
    # mean before they read the numbers. Written once, validated against
    # Trainline's real basket flow on 2026-04-24 for 15 Sep and 9 Jun.
    explainer = (
        '<div class="patterns-explainer">'
        '<strong>How pricing works on this route</strong><br>'
        'The cheapest <strong>07:36 outward</strong> price Trainline offers '
        'is always <strong>SplitSave</strong> — 2 tickets that together cover '
        'Yatton → Paddington, stay on the <em>same train</em> (no changes), '
        'refundable until 23:59 the day before travel. The <strong>18:30 '
        'return</strong> is an Advance Single at £27, specified-train, no '
        'refunds. Trainline adds a flat <strong>~£2.79 booking fee</strong> '
        'at checkout, so every total on this page is shown as '
        '"tickets + fee = all-in".'
        '</div>'
    )

    return (
        '<div class="patterns">'
        '<div class="patterns-head">What we know about this route</div>'
        f'{explainer}'
        f'<ul class="patterns-list">{items}</ul>'
        '</div>'
    )


# ---------- hero / headline ----------

# Hero variants — each one has a distinct colour palette so the state is
# readable at a glance.
_HERO_STYLES = {
    "urgent":  "background:#fef3c7; border-left-color:#b45309; color:#7c2d12;",  # amber — act now
    "buy":     "background:#ecfdf3; border-left-color:#067647; color:#064e2f;",  # green — good price
    "watch":   "background:#eff6ff; border-left-color:#175cd3; color:#1c3a6e;",  # blue — neutral
    "hold":    "background:#fef3f2; border-left-color:#b42318; color:#7a271a;",  # red — wait
    "quiet":   "background:#f7f5ee; border-left-color:#707070; color:#3a3a3a;",  # grey — no news
}


def _hero_html(style_key: str, body: str) -> str:
    style = _HERO_STYLES.get(style_key, _HERO_STYLES["quiet"])
    return f'<div class="hero" style="{style}">{body}</div>'


def _compose_hero(data: dict) -> str:
    """Today-specific prescriptive headline. Reads last_run.movements and
    patterns so every line refers to concrete dates and numbers from today's
    actual scrape — never generic filler.

    Priority of signals (most actionable first):
      1. NEW LOWS → "book these today, beaten prior lows"
      2. Dates currently at the route's all-time low total → "book these today"
      3. Bulk DROP event → "window opened, book affected dates today"
      4. Bulk RISE event → "prices stepped up, wait for them to drop back"
      5. Outliers without bulk context → "single unusual move — check"
      6. Stable, priced above route median → "running expensive, wait"
      7. Stable, at/below route median → quiet-day note, refer to patterns
    """
    last_run = data.get("last_run") or {}
    movements = last_run.get("movements") or {}
    patterns = data.get("patterns") or {}
    tuesdays = [t for t in (data.get("tuesdays") or []) if not t.get("booked")]

    new_lows = movements.get("new_lows") or []
    bulk_events = movements.get("bulk_events") or []
    outliers = movements.get("outliers") or []
    route_min = patterns.get("route_min")
    route_median = patterns.get("route_median")

    # Totals currently showing — used for "at route min" and "above median" flags.
    current_totals = [
        ((t.get("current") or {}).get("cheapest_any_total"), t["date"])
        for t in tuesdays
        if isinstance((t.get("current") or {}).get("cheapest_any_total"), (int, float))
    ]

    # --- 1. New historical lows ------------------------------------------
    if new_lows:
        dates_txt = ", ".join(
            f"<strong>{_fmt_date_short(nl['date'])}</strong> "
            f"({_fmt_gbp2(_all_in(nl['total']))} all-in)"
            for nl in new_lows[:4]
        )
        more = f" +{len(new_lows) - 4} more" if len(new_lows) > 4 else ""
        return _hero_html(
            "buy",
            f"🎯 <strong>New all-time low{'s' if len(new_lows) > 1 else ''}:</strong> "
            f"{dates_txt}{more}. Book today — these beat every prior observation we've recorded. "
            f'<span class="hero-sub">Prices are SplitSave (2 tickets, same train, day-before refund) inc. ~£2.79 Trainline fee.</span>'
        )

    # --- 2. Dates at route-min (not strictly a NEW low but still the min) --
    if isinstance(route_min, (int, float)):
        at_min = [d for tot, d in current_totals if tot is not None and tot <= route_min + 0.01]
        if at_min:
            n = len(at_min)
            preview = ", ".join(_fmt_date_short(d) for d in at_min[:4])
            more = f" +{n - 4} more" if n > 4 else ""
            return _hero_html(
                "buy",
                f"💰 <strong>{n} Tuesday{'s' if n != 1 else ''} at the all-time low "
                f"({_fmt_gbp2(_all_in(route_min))} all-in)</strong>: {preview}{more}. "
                f"Book these today — we've never seen cheaper on this route. "
                f'<span class="hero-sub">SplitSave tickets (2 tickets, same train, day-before refund) + ~£2.79 Trainline fee.</span>'
            )

    # --- 3. Bulk DROP ----------------------------------------------------
    drops = [e for e in bulk_events if e["delta"] < 0]
    if drops:
        ev = max(drops, key=lambda e: e["count"])
        leg_label = "07:36 SplitSave outward" if ev["leg"] == "outward" else "18:30 return"
        date_span = _date_span_phrase(ev["dates"])
        return _hero_html(
            "buy",
            f"📉 <strong>{ev['count']} Tuesday{'s' if ev['count'] != 1 else ''} just dropped "
            f"{_fmt_gbp(abs(ev['delta']))}</strong> on the {leg_label} "
            f"({_fmt_gbp(ev['from'])} → {_fmt_gbp(ev['to'])}) — {date_span}. "
            f"Lower rung of the Advance-fare seesaw; good window to book before it flips back. "
            f'<span class="hero-sub">All prices above are ticket-only; add ~£2.79 Trainline fee at checkout.</span>'
        )

    # --- 4. Bulk RISE ----------------------------------------------------
    rises = [e for e in bulk_events if e["delta"] > 0]
    if rises:
        ev = max(rises, key=lambda e: e["count"])
        leg_label = "07:36 SplitSave outward" if ev["leg"] == "outward" else "18:30 return"
        date_span = _date_span_phrase(ev["dates"])
        return _hero_html(
            "hold",
            f"⚠️ <strong>{ev['count']} Tuesday{'s' if ev['count'] != 1 else ''} stepped up "
            f"{_fmt_gbp(abs(ev['delta']))}</strong> on the {leg_label} "
            f"({_fmt_gbp(ev['from'])} → {_fmt_gbp(ev['to'])}) — {date_span}. "
            f"Don't buy today; history says these cycle back down within days."
        )

    # --- 5. Outliers without any bulk ------------------------------------
    if outliers:
        o = outliers[0]
        pieces = []
        if isinstance(o.get("delta_out"), (int, float)) and abs(o["delta_out"]) >= 0.01:
            arrow = "↓" if o["delta_out"] < 0 else "↑"
            pieces.append(
                f"07:36 out {arrow} {_fmt_gbp(abs(o['delta_out']))} "
                f"({_fmt_gbp(o['from_out'])} → {_fmt_gbp(o['to_out'])})"
            )
        if isinstance(o.get("delta_back"), (int, float)) and abs(o["delta_back"]) >= 0.01:
            arrow = "↓" if o["delta_back"] < 0 else "↑"
            pieces.append(
                f"18:30 back {arrow} {_fmt_gbp(abs(o['delta_back']))}"
            )
        desc = " · ".join(pieces) or "leg fares moved"
        return _hero_html(
            "watch",
            f"🔍 <strong>{_fmt_date_short(o['date'])}</strong> moved on its own today — "
            f"{desc}. Worth a quick look — may be a one-date pricing quirk."
        )

    # --- 6. Stable, running above median --------------------------------
    if isinstance(route_median, (int, float)) and current_totals:
        above = [d for tot, d in current_totals if tot is not None and tot > route_median + 0.5]
        if above and len(above) >= max(3, len(current_totals) // 2):
            return _hero_html(
                "hold",
                f"Fares running <strong>above the £{route_median:.0f} median</strong> "
                f"on {len(above)} of {len(current_totals)} tracked Tuesdays. "
                f"Nothing urgent — waiting a few days costs nothing."
            )

    # --- 7. Quiet day ----------------------------------------------------
    # Describe where we actually are rather than say "sit tight".
    if isinstance(route_median, (int, float)) and isinstance(route_min, (int, float)):
        at_or_below_median = sum(
            1 for tot, _ in current_totals
            if tot is not None and tot <= route_median + 0.5
        )
        return _hero_html(
            "quiet",
            f"Quiet day — no day-over-day moves. {at_or_below_median} of "
            f"{len(current_totals)} Tuesdays at or below the "
            f"{_fmt_gbp2(_all_in(route_median))} all-in median; "
            f"all-time low on record is {_fmt_gbp2(_all_in(route_min))} all-in."
        )
    # Fallback — first run, no patterns yet.
    return _hero_html(
        "quiet",
        f"{len(current_totals)} Tuesday{'s' if len(current_totals) != 1 else ''} tracked. "
        f"Trend data builds as we accumulate more daily checks."
    )


def _date_span_phrase(dates: list[str]) -> str:
    """Return 'Jun–Aug' style range when dates span multiple months, or an
    explicit list if ≤3 dates. Helps the hero stay scannable on bulk events."""
    if not dates:
        return ""
    if len(dates) <= 3:
        return ", ".join(_fmt_date_short(d) for d in dates)
    months = sorted({d[:7] for d in dates})  # YYYY-MM
    first = datetime.strptime(months[0] + "-01", "%Y-%m-%d").strftime("%b")
    last = datetime.strptime(months[-1] + "-01", "%Y-%m-%d").strftime("%b")
    span = first if first == last else f"{first}–{last}"
    return f"{span} {datetime.strptime(dates[0], '%Y-%m-%d').year}"


# ---------- main render ----------

CSS = """
  :root {
    --bg: #fafaf7;
    --ink: #1a1a1a;
    --muted: #6b6b6b;
    --rule: #e6e3db;
    --card: #ffffff;
    --urgent: #b42318;
    --urgent-bg: #fef3f2;
    --today: #b54708;
    --today-bg: #fef9e7;
    --soon: #175cd3;
    --soon-bg: #eff6ff;
    --stable: #3f7d3f;
    --stable-bg: #f0faf0;
    --pending: #707070;
    --pending-bg: #f2f0eb;
    --save: #067647;
    --save-bg: #ecfdf3;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "SF Pro Text", Roboto, sans-serif; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 28px 20px 80px; }
  header { margin-bottom: 8px; }
  h1 { font-size: 24px; margin: 0 0 4px; letter-spacing: -0.01em; }
  .subtitle { font-size: 14px; color: var(--muted); margin: 0; }
  .hero { margin: 22px 0 26px; padding: 18px 20px; background: var(--urgent-bg); border: 1px solid #fcd6d1; border-left: 4px solid var(--urgent); border-radius: 10px; color: var(--urgent); font-size: 15px; line-height: 1.5; }
  .hero strong { color: var(--urgent); }
  .section-title { font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin: 30px 0 12px; font-weight: 600; }
  .card { background: var(--card); border: 1px solid var(--rule); border-radius: 10px; padding: 16px 18px; margin-bottom: 12px; }
  .card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }
  .date { font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
  .weeks-out { font-size: 12px; color: var(--muted); margin-left: 8px; font-weight: 400; }
  .status { font-size: 10.5px; letter-spacing: 0.1em; text-transform: uppercase; font-weight: 700; padding: 4px 9px; border-radius: 999px; }
  .status.urgent { background: var(--urgent-bg); color: var(--urgent); }
  .status.today { background: var(--today-bg); color: var(--today); }
  .status.soon { background: var(--soon-bg); color: var(--soon); }
  .status.stable { background: var(--stable-bg); color: var(--stable); }
  .status.booked { background: #eeece6; color: #8a8a8a; }
  .card.booked { opacity: 0.55; background: #f6f4ee; }
  .card.booked .date { color: #6b6b6b; }
  .legs-summary { padding: 12px; background: #fdfcf9; border: 1px solid var(--rule); border-radius: 8px; font-size: 14px; color: var(--muted); text-align: center; margin-bottom: 10px; }
  .legs-summary strong { color: var(--ink); font-size: 18px; font-weight: 600; }
  .outward-hero { padding: 14px 16px; background: var(--soon-bg); border: 1px solid #cfe0f4; border-radius: 8px; margin-bottom: 6px; }
  .outward-label { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-weight: 700; }
  .outward-label strong { color: var(--ink); font-weight: 700; }
  .outward-fare { font-size: 30px; font-weight: 700; color: var(--ink); letter-spacing: -0.02em; margin-top: 4px; line-height: 1; }
  .outward-meta { font-size: 12px; color: var(--muted); margin-top: 6px; }
  .return-foot { padding: 8px 14px; font-size: 13px; color: var(--muted); background: #fdfcf9; border: 1px solid var(--rule); border-radius: 8px; margin: 6px 0 10px; }
  .return-foot strong { color: var(--ink); font-weight: 600; }
  .totals { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; background: #f7f5ee; border-radius: 8px; font-size: 13.5px; margin-bottom: 10px; gap: 12px; flex-wrap: wrap; }
  .totals .over { color: var(--urgent); font-weight: 600; }
  .totals .base { color: var(--stable); font-weight: 600; }
  .alts { background: var(--save-bg); border: 1px solid #b7e4c7; border-radius: 8px; padding: 10px 12px; font-size: 13px; color: #064e2f; margin-bottom: 10px; }
  .alts-head { font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 700; color: var(--save); margin-bottom: 4px; }
  .alts ul { margin: 0; padding-left: 18px; }
  .alts li { margin: 2px 0; }
  .save-badge { display: inline-block; background: var(--save); color: white; font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 4px; margin-left: 4px; }
  .save-badge.up { background: var(--urgent); }
  .new-low-badge { display: inline-block; background: #fef3c7; color: #92400e; font-size: 10.5px; font-weight: 700; letter-spacing: 0.04em; padding: 2px 7px; border-radius: 4px; margin-left: 4px; border: 1px solid #fcd34d; }
  .moves { margin: 22px 0 16px; padding: 16px 18px; background: #fdfcf9; border: 1px solid var(--rule); border-left: 4px solid #3a3a3a; border-radius: 10px; }
  .moves-head { font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; font-weight: 700; }
  .moves-list { margin: 0; padding-left: 18px; font-size: 14px; line-height: 1.55; color: var(--ink); }
  .moves-list > li { margin: 6px 0; }
  .moves-list .move-bulk { list-style: '📉  '; }
  .moves-list .move-low { list-style: none; margin-left: -18px; padding-left: 0; }
  .moves-list .move-outlier { list-style: none; margin-left: -18px; padding-left: 0; }
  .move-count { color: var(--muted); font-weight: 500; }
  .move-dates { color: var(--muted); font-size: 13px; }
  .move-context { display: block; color: var(--muted); font-size: 12.5px; margin-top: 2px; font-style: italic; }
  .moves-quiet { margin: 22px 0 16px; padding: 12px 16px; background: var(--stable-bg); border: 1px solid #c6e5c6; border-left: 4px solid var(--stable); border-radius: 10px; font-size: 13.5px; color: var(--stable); }
  .moves-quiet-inline { margin-top: 6px; font-size: 12.5px; color: var(--muted); }
  .patterns { margin: 32px 0 8px; padding: 16px 18px; background: #f7f5ee; border: 1px solid var(--rule); border-radius: 10px; }
  .patterns-head { font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; font-weight: 700; }
  .patterns-list { margin: 0; padding-left: 18px; font-size: 13.5px; line-height: 1.55; color: var(--ink); }
  .patterns-list > li { margin: 5px 0; }
  .patterns-list .patterns-meta { list-style: none; margin-left: -18px; padding-left: 0; color: var(--muted); font-size: 12px; margin-top: 8px; }
  .patterns-explainer { font-size: 13px; line-height: 1.55; color: var(--ink); padding: 10px 12px; background: #fdfcf9; border: 1px solid var(--rule); border-radius: 8px; margin-bottom: 12px; }
  .patterns-explainer strong { color: var(--ink); }
  .hero-sub { display: block; font-size: 12.5px; font-weight: 400; margin-top: 6px; opacity: 0.85; }
  .note { font-size: 13px; line-height: 1.5; color: #3a3a3a; margin: 8px 0 12px; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .btn { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; padding: 12px 18px; font-size: 14px; font-weight: 600; border-radius: 8px; text-decoration: none; line-height: 1.2; }
  .btn-primary { background: var(--ink); color: #fff; flex: 1 1 auto; }
  .btn-secondary { background: #f0ede5; color: var(--ink); border: 1px solid var(--rule); }
  .pending-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; margin-top: 12px; }
  .pending-card { background: var(--card); border: 1px solid var(--rule); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; }
  .pending-date { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
  .pending-release { font-size: 12.5px; color: var(--muted); line-height: 1.4; }
  .pending-release strong { color: var(--ink); font-weight: 600; }
  .pending-btn { display: inline-flex; align-items: center; justify-content: center; min-height: 44px; margin-top: 6px; padding: 12px 14px; font-size: 13px; font-weight: 600; text-align: center; background: var(--ink); color: #fff; border-radius: 8px; text-decoration: none; }
  footer { margin-top: 36px; padding-top: 18px; border-top: 1px solid var(--rule); font-size: 12px; color: var(--muted); line-height: 1.6; }
  footer a { color: var(--muted); }
"""


def render_html(data: dict) -> str:
    tuesdays = data.get("tuesdays") or []
    last_run = data.get("last_run") or {}
    movements = last_run.get("movements") or {}
    per_tuesday_moves = movements.get("per_tuesday") or {}
    new_lows_by_date = {
        nl["date"]: nl for nl in (movements.get("new_lows") or [])
    }
    patterns = data.get("patterns") or {}
    route_median = patterns.get("route_median")

    # Dates that participate in a bulk event today — the moves banner
    # already explains them as a single pricing event, so we suppress the
    # identical per-card pill to stop painting the same delta 13 times.
    # Outliers stay loud (they're date-specific news), new-lows stay loud.
    suppress_pill_dates: set[str] = set()
    for ev in (movements.get("bulk_events") or []):
        for d in (ev.get("dates") or []):
            suppress_pill_dates.add(d)

    # Group by status in explicit order
    by_status = {}
    for t in tuesdays:
        by_status.setdefault(t.get("status", "UNKNOWN"), []).append(t)

    sections_html = []
    for status, title in SECTION_ORDER:
        group = by_status.get(status, [])
        if not group:
            continue
        group.sort(key=lambda t: t["date"])
        cards = "\n".join(
            _render_bookable_card(
                t,
                movement_ctx=per_tuesday_moves.get(t["date"]),
                new_lows_by_date=new_lows_by_date,
                route_median=route_median,
                suppress_pill_dates=suppress_pill_dates,
            )
            for t in group
        )
        sections_html.append(
            f'<div class="section-title">{html.escape(title)}</div>\n{cards}'
        )

    pending = sorted(data.get("not_bookable_yet") or [])
    pending_html = "\n".join(_render_pending_card(p) for p in pending) if pending else ""
    pending_section = ""
    if pending_html:
        pending_section = f"""
<div class="section-title">Not bookable yet · tap to add to Reminders</div>
<div class="card">
  <div class="note" style="margin-top:0">These Tuesdays unlock one at a time as Trainline's ~6-month booking window rolls forward (179 days ahead). Tap <strong>Add to Reminders</strong> and your iPhone's Reminders app will drop in a to-do with a timed alert, a 15-minute warning, and a direct Trainline link in the notes so you can book the second they go live.</div>
  <div class="pending-grid">
{pending_html}
  </div>
</div>
""".strip()

    refreshed = datetime.now().astimezone().strftime("%a %-d %b %Y, %H:%M %Z")
    hero = _compose_hero(data)
    moves_banner = _render_movements_banner(last_run)
    patterns_panel = _render_patterns_panel(patterns)

    # Source note — flag if NR fallback was used
    sources_used = {(t.get("current") or {}).get("source") for t in tuesdays}
    source_caveat = ""
    if "national_rail" in sources_used and "trainline" not in sources_used:
        source_caveat = (
            " · <strong>Source today: National Rail</strong> "
            "(Trainline unreachable — SplitSave totals may be lower when you tap through)"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Sophie's Train Tracker · Yatton ↔ Paddington</title>
<meta name="description" content="Daily prescriptive recommendations for Sophie's weekly Yatton → London Paddington Tuesday commute." />
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>Sophie's Train Tracker</h1>
  <p class="subtitle">Yatton → London Paddington · every Tuesday · adult, no railcard</p>
  <p class="subtitle" style="margin-top:4px;">Out no later than <strong>07:36</strong> · return no earlier than <strong>18:30</strong></p>
</header>

{hero}

{moves_banner}

{chr(10).join(sections_html)}

{pending_section}

{patterns_panel}

<footer>
  <p>Last refreshed <strong>{refreshed}</strong> · refreshed daily at 02:00 UK{source_caveat}.</p>
  <p>Source: <a href="https://www.thetrainline.com/train-times/yatton-to-london-paddington" target="_blank" rel="noopener">Trainline · Yatton → Paddington</a>. Fares reflect cheapest Advance tier at time of check.</p>
</footer>

</div>
</body>
</html>
"""


# ---------- ICS (reminders) ----------

def _ics_for_pending(iso_date: str) -> str:
    travel = datetime.strptime(iso_date, "%Y-%m-%d").date()
    release = travel - timedelta(days=HORIZON_DAYS)
    out_url = _trainline_url(iso_date, "out", "07:36")
    back_url = _trainline_url(iso_date, "back", "18:30")
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_dt = release.strftime("%Y%m%dT020000")
    end_dt = release.strftime("%Y%m%dT023000")
    travel_long = travel.strftime("%A %-d %B %Y")
    travel_short = travel.strftime("%-d %b")
    description = (
        f"Tickets for {travel_long} (Yatton → London Paddington) release around this time."
        f"\\n\\nTarget times: out 07:36 (peak)\\, back 18:30."
        f"\\nBaseline cost: £127 (£100 out + £27 back)."
        f"\\nNo price history yet — tickets haven't released."
        f"\\n\\nBOOK NOW (opens Trainline):"
        f"\\nOutbound: {out_url}"
        f"\\nReturn: {back_url}"
        f"\\n\\nFull tracker: {TRACKER_URL}"
    )
    return (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Sophie Train Tracker//EN\n"
        "CALSCALE:GREGORIAN\n"
        "METHOD:PUBLISH\n"
        "BEGIN:VTIMEZONE\n"
        "TZID:Europe/London\n"
        "BEGIN:DAYLIGHT\n"
        "TZOFFSETFROM:+0000\n"
        "TZOFFSETTO:+0100\n"
        "TZNAME:BST\n"
        "DTSTART:19700329T010000\n"
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\n"
        "END:DAYLIGHT\n"
        "BEGIN:STANDARD\n"
        "TZOFFSETFROM:+0100\n"
        "TZOFFSETTO:+0000\n"
        "TZNAME:GMT\n"
        "DTSTART:19701025T020000\n"
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\n"
        "END:STANDARD\n"
        "END:VTIMEZONE\n"
        "BEGIN:VEVENT\n"
        f"UID:sophie-{iso_date}@train-tracker\n"
        f"DTSTAMP:{dtstamp}\n"
        f"DTSTART;TZID=Europe/London:{start_dt}\n"
        f"DTEND;TZID=Europe/London:{end_dt}\n"
        f"SUMMARY:Book Sophie's train · Tue {travel_short}\n"
        f"DESCRIPTION:{description}\n"
        f"URL:{out_url}\n"
        "STATUS:CONFIRMED\n"
        "TRANSP:TRANSPARENT\n"
        "BEGIN:VALARM\n"
        "ACTION:DISPLAY\n"
        f"DESCRIPTION:Tickets now on sale for {travel_long} — book Sophie's train\n"
        "TRIGGER:PT0M\n"
        "END:VALARM\n"
        "BEGIN:VALARM\n"
        "ACTION:DISPLAY\n"
        f"DESCRIPTION:Tickets about to release for {travel_long}\n"
        "TRIGGER:-PT15M\n"
        "END:VALARM\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )


def regenerate_reminders(pending: list):
    REMINDERS_DIR.mkdir(exist_ok=True)
    # Clean up old ICS files no longer in the pending list (ignore permission errors —
    # bookable-now dates may have stale ICS that we can't unlink from the sandbox)
    want = set(f"{d}.ics" for d in pending)
    for existing in REMINDERS_DIR.glob("*.ics"):
        if existing.name not in want:
            try:
                existing.unlink()
            except (PermissionError, OSError):
                pass
    for d in pending:
        try:
            (REMINDERS_DIR / f"{d}.ics").write_text(_ics_for_pending(d))
        except (PermissionError, OSError):
            pass


# ---------- main ----------

def main():
    data = json.loads(PRICES.read_text())
    html_text = render_html(data)
    INDEX.write_text(html_text)
    ARTIFACT.write_text(html_text)
    regenerate_reminders(data.get("not_bookable_yet") or [])
    print(f"Wrote {INDEX} ({len(html_text)} chars)")
    print(f"Wrote {ARTIFACT}")
    print(f"Regenerated {len(data.get('not_bookable_yet') or [])} ICS files in {REMINDERS_DIR}")


if __name__ == "__main__":
    main()
