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

# Trainline deep-link station hashes (copied from existing template)
YAT = "1551a38ad87e8710d21b25403ae0a3e6"
PAD = "1f06fc66ccd7ea92ae4b0a550e4ddfd1"
DOB = "1995-01-01"

STATUS_LABELS = {
    "URGENT": ("Cheap tier gone", "urgent"),
    "BOOK_TODAY": ("Book today", "today"),
    "BOOK_SOON": ("Book soon", "soon"),
    "STABLE": ("Watch · at baseline", "stable"),
    "UNKNOWN": ("Awaiting data", "stable"),
    "BOOKED": ("Already booked", "booked"),
}

SECTION_ORDER = [
    ("URGENT", "Urgent · cheap tier gone"),
    ("BOOK_TODAY", "Book this week · last cheap tier"),
    ("BOOK_SOON", "Book soon · within 1–2 weeks"),
    ("STABLE", "Baseline · sit tight, book in the next fortnight"),
    ("UNKNOWN", "Awaiting data"),
    ("BOOKED", "Already booked · paid tickets"),
]


# ---------- formatting helpers ----------

def _fmt_gbp(x):
    if x is None:
        return "—"
    return f"£{x:.0f}" if x == int(x) else f"£{x:.2f}"


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


# ---------- card rendering ----------

def _arrow_badge(change: dict) -> str:
    """Tiny pill showing yesterday's direction of travel if notable."""
    if not change:
        return ""
    delta = change.get("cheapest_any")
    if delta is None or abs(delta) < 0.01:
        return ""
    if delta < 0:
        return f' <span class="save-badge">↓ {_fmt_gbp(abs(delta))} vs yesterday</span>'
    return (
        f' <span class="save-badge" style="background:#b42318">'
        f'↑ {_fmt_gbp(delta)} vs yesterday</span>'
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


def _totals_row(cur: dict, baseline: float) -> str:
    total = cur.get("cheapest_any_total")
    if total is None:
        return '<div class="totals"><span>No total yet</span></div>'
    if total > baseline + 0.5:
        flag = f'<span class="over">{_fmt_gbp(total - baseline)} over baseline</span>'
    elif total < baseline - 0.5:
        flag = f'<span class="base">{_fmt_gbp(baseline - total)} under baseline</span>'
    else:
        flag = '<span class="base">at baseline</span>'
    return f'<div class="totals"><span>Total <strong>{_fmt_gbp(total)}</strong></span>{flag}</div>'


def _leg_html(leg: dict, label: str) -> str:
    if not leg:
        return (
            f'<div class="leg"><div class="leg-label">{label}</div>'
            f'<div class="leg-time">—</div><div class="leg-fare">No data</div></div>'
        )
    t = leg.get("time") or "—"
    f = leg.get("fare")
    changes = leg.get("changes")
    bits = [_fmt_gbp(f)]
    if changes == 0:
        bits.append("direct")
    elif changes:
        bits.append(f"{changes} change{'s' if changes > 1 else ''}")
    return (
        f'<div class="leg"><div class="leg-label">{label}</div>'
        f'<div class="leg-time">{html.escape(t)}</div>'
        f'<div class="leg-fare">{html.escape(" · ".join(bits))}</div></div>'
    )


def _render_bookable_card(t: dict) -> str:
    cur = t.get("current") or {}
    status = t.get("status", "UNKNOWN")
    status_label, status_class = STATUS_LABELS.get(status, STATUS_LABELS["UNKNOWN"])
    wk = _weeks_out(t["date"])
    out_leg = cur.get("out")
    back_leg = cur.get("back")
    out_time = (out_leg or {}).get("time") or "07:36"
    back_time = (back_leg or {}).get("time") or "18:30"
    note = t.get("note") or ""
    source_note = cur.get("_note_on_source")
    # Append source caveat if we're on NR fallback
    full_note = note
    if source_note:
        # Only show if note doesn't already mention the same idea
        if "National Rail" not in note and "SplitSave" not in note:
            full_note = (note + " " if note else "") + source_note
    change_badge = _arrow_badge(t.get("change_vs_yesterday"))
    is_booked = bool(t.get("booked"))

    # When leg-level data is missing (headline-only scrape), avoid showing
    # two "No data" blocks. Fall back to a single summary row highlighting
    # the cheapest total — honest about the limit, quieter visually.
    if out_leg is None and back_leg is None:
        total = cur.get("cheapest_any_total")
        if total is not None:
            legs_block = (
                '<div class="legs-summary">'
                f'Cheapest return: <strong>{_fmt_gbp(total)}</strong> · per-leg times refresh when you tap through'
                '</div>'
            )
        else:
            legs_block = (
                '<div class="legs-summary">Price not captured this run — re-checking tomorrow</div>'
            )
    else:
        legs_block = (
            '<div class="legs">'
            f'{_leg_html(out_leg, "Out")}'
            f'{_leg_html(back_leg, "Back")}'
            '</div>'
        )

    if is_booked:
        actions_block = (
            '<div class="note" style="color:#8a8a8a; font-style:italic;">'
            'Tickets already paid for — no action needed.'
            '</div>'
        )
    else:
        actions_block = (
            '<div class="actions">'
            f'<a class="btn btn-primary" href="{_trainline_url(t["date"], "out", out_time)}" target="_blank" rel="noopener">Book outbound ({html.escape(out_time)}) →</a>'
            f'<a class="btn btn-secondary" href="{_trainline_url(t["date"], "back", back_time)}" target="_blank" rel="noopener">Book return ({html.escape(back_time)})</a>'
            '</div>'
        )

    card_class = "card booked" if is_booked else "card"
    return f"""
<div class="{card_class}">
  <div class="card-head">
    <div><span class="date">{_fmt_date_short(t['date'])}</span><span class="weeks-out">{wk} week{'s' if wk != 1 else ''} out</span>{change_badge}</div>
    <div class="status {status_class}">{html.escape(status_label)}</div>
  </div>
  {legs_block}
  {_totals_row(cur, t.get('baseline_total') or 127.0)}
  {_alts_block(cur)}
  <div class="note">{html.escape(full_note)}</div>
  {actions_block}
</div>
""".strip()


def _render_pending_card(iso_date: str) -> str:
    # Ticket release is ~12 weeks before travel date. Show the release date.
    travel = datetime.strptime(iso_date, "%Y-%m-%d").date()
    release = travel - timedelta(weeks=12)
    release_txt = release.strftime("%-d %b")
    return f"""
    <div class="pending-card">
      <div class="pending-date">{_fmt_date_short(iso_date)}</div>
      <div class="pending-release">Releases around <strong>{release_txt}</strong></div>
      <a class="pending-btn" href="reminders/{iso_date}.ics" download>Add to Reminders</a>
    </div>
""".rstrip()


# ---------- hero / headline ----------

def _compose_hero(data: dict) -> str:
    summary = data.get("summary") or {}
    headline = summary.get("headline")
    if headline:
        return f'<div class="hero">{html.escape(headline)}</div>'
    over = summary.get("currently_paying_over_baseline") or []
    if over:
        parts = [f"<strong>{_fmt_date_short(o['date'])}</strong> ({_fmt_gbp(o['over'])} over)" for o in over[:3]]
        txt = "Currently over baseline: " + ", ".join(parts) + ". Book these today to cap the damage."
        return f'<div class="hero">{txt}</div>'
    return (
        '<div class="hero" style="background:#f0faf0; border-left-color:#3f7d3f; color:#3f7d3f;">'
        'All tracked Tuesdays at or below baseline — sit tight. I\'ll check again tomorrow.'
        '</div>'
    )


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
  .legs { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
  .leg { border: 1px solid var(--rule); border-radius: 8px; padding: 10px 12px; background: #fdfcf9; }
  .leg-label { font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
  .leg-time { font-size: 19px; font-weight: 600; margin-top: 3px; }
  .leg-fare { font-size: 13px; color: var(--muted); margin-top: 2px; }
  .totals { display: flex; justify-content: space-between; padding: 10px 12px; background: #f7f5ee; border-radius: 8px; font-size: 14px; margin-bottom: 10px; }
  .totals .over { color: var(--urgent); font-weight: 600; }
  .totals .base { color: var(--stable); font-weight: 600; }
  .alts { background: var(--save-bg); border: 1px solid #b7e4c7; border-radius: 8px; padding: 10px 12px; font-size: 13px; color: #064e2f; margin-bottom: 10px; }
  .alts-head { font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; font-weight: 700; color: var(--save); margin-bottom: 4px; }
  .alts ul { margin: 0; padding-left: 18px; }
  .alts li { margin: 2px 0; }
  .save-badge { display: inline-block; background: var(--save); color: white; font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 4px; margin-left: 4px; }
  .note { font-size: 13px; line-height: 1.5; color: #3a3a3a; margin: 8px 0 12px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .btn { display: inline-block; padding: 8px 14px; font-size: 13px; font-weight: 600; border-radius: 6px; text-decoration: none; }
  .btn-primary { background: var(--ink); color: #fff; }
  .btn-secondary { background: #f0ede5; color: var(--ink); border: 1px solid var(--rule); }
  .pending-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; margin-top: 12px; }
  .pending-card { background: var(--card); border: 1px solid var(--rule); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; }
  .pending-date { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
  .pending-release { font-size: 12.5px; color: var(--muted); line-height: 1.4; }
  .pending-release strong { color: var(--ink); font-weight: 600; }
  .pending-btn { display: inline-block; margin-top: 4px; padding: 8px 10px; font-size: 12.5px; font-weight: 600; text-align: center; background: var(--ink); color: #fff; border-radius: 6px; text-decoration: none; }
  footer { margin-top: 36px; padding-top: 18px; border-top: 1px solid var(--rule); font-size: 12px; color: var(--muted); line-height: 1.6; }
  footer a { color: var(--muted); }
"""


def render_html(data: dict) -> str:
    tuesdays = data.get("tuesdays") or []
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
        cards = "\n".join(_render_bookable_card(t) for t in group)
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
  <div class="note" style="margin-top:0">These Tuesdays unlock one at a time as the 12-week window rolls forward. Tap <strong>Add to Reminders</strong> and your iPhone's Reminders app will drop in a to-do with a timed alert, a 15-minute warning, and a direct Trainline link in the notes so you can book the second they go live.</div>
  <div class="pending-grid">
{pending_html}
  </div>
</div>
""".strip()

    refreshed = datetime.now().astimezone().strftime("%a %-d %b %Y, %H:%M %Z")
    hero = _compose_hero(data)

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

{chr(10).join(sections_html)}

{pending_section}

<footer>
  <p>Last refreshed <strong>{refreshed}</strong> · refreshed daily at 02:00 UK{source_caveat}.</p>
  <p>Source: <a href="https://www.thetrainline.com/train-times/yatton-to-london-paddington" target="_blank" rel="noopener">Trainline · Yatton → Paddington</a> (primary) · <a href="https://ojp.nationalrail.co.uk/" target="_blank" rel="noopener">National Rail</a> (fallback). Fares reflect cheapest Advance tier at time of check.</p>
</footer>

</div>
</body>
</html>
"""


# ---------- ICS (reminders) ----------

def _ics_for_pending(iso_date: str) -> str:
    travel = datetime.strptime(iso_date, "%Y-%m-%d").date()
    release = travel - timedelta(weeks=12)
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
