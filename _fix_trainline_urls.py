#!/usr/bin/env python3
"""One-off fix: swap broken ojp.nationalrail.co.uk booking URLs in
_template.html for Trainline /dpi URLs that actually deep-link on
iOS Safari + Chrome.

Trainline's canonical deep-link format (harvested from their own
station-page calendar 22 Apr 2026):

  https://www.thetrainline.com/dpi?locale=en
    &origin=<hash>&destination=<hash>
    &outboundType=departAfter
    &outboundTime=YYYY-MM-DDTHH%3AMM%3A00
    &affiliateCode=tlseo&currency=GBP
    &passengers%5B0%5D%5Bdob%5D=1995-01-01
    &journeyType=single

Station hashes:
  Yatton            → 1551a38ad87e8710d21b25403ae0a3e6
  London Paddington → 1f06fc66ccd7ea92ae4b0a550e4ddfd1
"""
import re
from pathlib import Path
from urllib.parse import quote

TPL = Path("/sessions/cool-determined-mayer/mnt/Train Tickets/_template.html")
YAT = "1551a38ad87e8710d21b25403ae0a3e6"
PAD = "1f06fc66ccd7ea92ae4b0a550e4ddfd1"


def trainline_url(origin: str, dest: str, iso_date: str, hhmm: str) -> str:
    iso = f"{iso_date}T{hhmm[:2]}:{hhmm[2:]}:00"
    iso_enc = quote(iso, safe="")
    return (
        "https://www.thetrainline.com/dpi?locale=en"
        f"&origin={origin}&destination={dest}"
        "&outboundType=departAfter"
        f"&outboundTime={iso_enc}"
        "&affiliateCode=tlseo&currency=GBP"
        "&passengers%5B0%5D%5Bdob%5D=1995-01-01"
        "&journeyType=single"
    )


def ddmmyy_to_iso(ddmmyy: str) -> str:
    return f"20{ddmmyy[4:]}-{ddmmyy[2:4]}-{ddmmyy[:2]}"


def offset_time(hhmm: str, minus_minutes: int = 6) -> str:
    """Return hhmm shifted back by N minutes so the target train is the
    first result, not borderline-missing."""
    h, m = int(hhmm[:2]), int(hhmm[2:])
    total = h * 60 + m - minus_minutes
    total = max(0, total)
    return f"{total // 60:02d}{total % 60:02d}"


lines = TPL.read_text().splitlines(keepends=False)

# State machine: track the last-seen outbound and return times per card.
# When we hit a primary/secondary booking button, use those times + the
# ddmmyy embedded in the button to build Trainline URLs.
out_time = None
ret_time = None
leg_seen = 0

leg_time_re = re.compile(r'<div class="leg-time">(\d{2}):(\d{2})</div>')
primary_re = re.compile(
    r'(\s*)<a class="btn btn-primary" href="https://ojp\.nationalrail\.co\.uk/service/timesandfares/YAT/PAD/(\d{6})/\d{4}/dep" target="_blank" rel="noopener">Book on National Rail →</a>'
)
secondary_re = re.compile(
    r'(\s*)<a class="btn btn-secondary" href="https://ojp\.nationalrail\.co\.uk/service/timesandfares/YAT/PAD/(\d{6})/\d{4}/dep" target="_blank" rel="noopener">National Rail</a>'
)
card_start_re = re.compile(r'<div class="card">')

new_lines = []
for line in lines:
    if card_start_re.search(line):
        out_time = None
        ret_time = None
        leg_seen = 0
        new_lines.append(line)
        continue

    m = leg_time_re.findall(line)
    if m:
        for hh, mm in m:
            if leg_seen == 0:
                out_time = f"{hh}{mm}"
            elif leg_seen == 1:
                ret_time = f"{hh}{mm}"
            leg_seen += 1
        new_lines.append(line)
        continue

    mp = primary_re.match(line)
    if mp and out_time is not None:
        indent, ddmmyy = mp.groups()
        iso_date = ddmmyy_to_iso(ddmmyy)
        out_hh_mm = f"{out_time[:2]}:{out_time[2:]}"
        url = trainline_url(YAT, PAD, iso_date, offset_time(out_time))
        new_lines.append(
            f'{indent}<a class="btn btn-primary" href="{url}" target="_blank" rel="noopener">Book outbound ({out_hh_mm}) →</a>'
        )
        continue

    ms = secondary_re.match(line)
    if ms and ret_time is not None:
        indent, ddmmyy = ms.groups()
        iso_date = ddmmyy_to_iso(ddmmyy)
        ret_hh_mm = f"{ret_time[:2]}:{ret_time[2:]}"
        url = trainline_url(PAD, YAT, iso_date, offset_time(ret_time))
        new_lines.append(
            f'{indent}<a class="btn btn-secondary" href="{url}" target="_blank" rel="noopener">Book return ({ret_hh_mm})</a>'
        )
        continue

    new_lines.append(line)

text = "\n".join(new_lines) + "\n"

# Bottom source-attribution link
text = text.replace(
    '<p>Source: <a href="https://ojp.nationalrail.co.uk/" target="_blank" rel="noopener">ojp.nationalrail.co.uk</a>',
    '<p>Source: <a href="https://www.thetrainline.com/train-times/yatton-to-london-paddington" target="_blank" rel="noopener">Trainline · Yatton → Paddington</a>',
)

TPL.write_text(text)
print(f"rewrote {TPL.name}")
print(f"remaining ojp.nationalrail references: {text.count('ojp.nationalrail')}")
print(f"trainline.com references: {text.count('thetrainline.com')}")
