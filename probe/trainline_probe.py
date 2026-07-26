#!/usr/bin/env python3
"""Feasibility probe: will Trainline serve real fares to a datacentre IP?

READ-ONLY. Touches no pipeline file. Exists to answer one question before we
commit to a cloud migration: the current scrape runs in Paddy's logged-in Chrome
on a UK home IP. GitHub Actions runners are Azure datacentre IPs, which bot
detection treats very differently.

Verdict lines are machine-readable — grep for "VERDICT:".

Possible verdicts:
  REAL_FARES     — containers hydrated with plausible rows. Cloud is viable.
  BOT_CHALLENGE  — interstitial / access denied / rate limited.
  CAPTCHA        — an actual human-verification challenge.
  COACH_REDIRECT — bounced to the coach tab (URL/horizon problem, not blocking).
  EMPTY          — page loaded, containers never hydrated.

If the verdict is CAPTCHA we STOP. We do not build around human-verification
challenges — that is a hard line, not a technical obstacle to route around.
Two dates are probed back-to-back to expose rate limiting on the second.
"""
from __future__ import annotations

import re
import sys

from playwright.sync_api import sync_playwright

ORIGIN = "YAT3392gb"        # Yatton
DEST = "PAD3087gb"          # London Paddington

# Two dates with fares we already know from the 26 Jul run, so a pass is
# provable rather than merely plausible.
CASES = [
    {"date": "2026-11-19", "expect_out": 43.8, "expect_back": 27.0},
    {"date": "2026-11-26", "expect_out": 43.8, "expect_back": 27.0},
]

CHALLENGE_MARKERS = [
    "access denied", "unusual traffic", "are you a robot", "bot detection",
    "request blocked", "rate limit", "too many requests", "pardon our interruption",
    "incapsula", "imperva", "perimeterx", "px-captcha", "cf-challenge",
    "attention required", "checking your browser",
]
CAPTCHA_MARKERS = ["recaptcha", "hcaptcha", "captcha", "verify you are human",
                   "i'm not a robot"]


def build_url(date: str) -> str:
    return (
        "https://www.thetrainline.com/book/results"
        "?journeySearchType=return"
        f"&origin=urn%3Atrainline%3Ageneric%3Aloc%3A{ORIGIN}"
        f"&destination=urn%3Atrainline%3Ageneric%3Aloc%3A{DEST}"
        f"&outwardDate={date}T07%3A00%3A00&outwardDateType=departAfter"
        f"&inwardDate={date}T18%3A00%3A00&inwardDateType=departAfter"
        "&selectedTab=train&splitSave=true&lang=en"
        "&transportModes%5B%5D=mixed"
    )


EXTRACT_JS = r"""
() => {
  const pp = s => {
    if (!s) return null;
    const m = s.match(/£\s*([\d,]+(?:\.\d+)?)/);
    return m ? parseFloat(m[1].replace(/,/g, '')) : null;
  };
  const pc = t => {
    const c = document.querySelector(`[data-test="${t}"]`);
    if (!c) return null;
    const rows = [];
    c.querySelectorAll('[data-test="train-results-departure-time"]').forEach(dt => {
      let row = dt;
      for (let j = 0; j < 8; j++) {
        if (!row.parentElement) break;
        row = row.parentElement;
        if (row.querySelector('[data-test="train-results-arrival-time"]') &&
            row.querySelector('[data-test="alternative-price"]')) break;
      }
      const de = row.querySelector('[data-test="train-results-departure-time"]');
      const ae = row.querySelector('[data-test="train-results-arrival-time"]');
      const pe = row.querySelector('[data-test="alternative-price"]');
      const dm = (de ? de.textContent : '').match(/\d{2}:\d{2}/);
      const am = (ae ? ae.textContent : '').match(/\d{2}:\d{2}/);
      rows.push({dep: dm ? dm[0] : null, arr: am ? am[0] : null,
                 price: pp(pe ? pe.textContent.trim() : '')});
    });
    return rows;
  };
  return {
    outward: pc('train-results-container-OUTWARD'),
    inward: pc('train-results-container-INWARD'),
    title: document.title,
    url: location.href,
    text: (document.body.innerText || '').slice(0, 4000),
  };
};
"""


def is_skeleton(rows) -> bool:
    """Same guard as the live extractor: Trainline renders placeholder rows
    (02:11 / £88.88) before real data lands."""
    if not rows:
        return True
    if len({r["dep"] for r in rows}) < 2:
        return True
    if all(r["price"] == 88.88 for r in rows):
        return True
    if all(r["dep"] == r["arr"] for r in rows):
        return True
    return False


def classify(res) -> str:
    low = (res["text"] or "").lower()
    if any(m in low for m in CAPTCHA_MARKERS):
        return "CAPTCHA"
    if any(m in low for m in CHALLENGE_MARKERS):
        return "BOT_CHALLENGE"
    if "selectedtab=coach" in res["url"].lower():
        return "COACH_REDIRECT"
    out, inw = res["outward"], res["inward"]
    if out and inw and not is_skeleton(out) and not is_skeleton(inw):
        return "REAL_FARES"
    return "EMPTY"


def probe(page, case) -> str:
    date = case["date"]
    url = build_url(date)
    print(f"\n=== {date} ===")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"  goto EXC {type(e).__name__}: {e}")
        return "EMPTY"

    # Poll for hydration the same way the live extractor does.
    verdict, res = "EMPTY", None
    for _ in range(45):
        res = page.evaluate(EXTRACT_JS)
        verdict = classify(res)
        if verdict in ("REAL_FARES", "CAPTCHA", "BOT_CHALLENGE", "COACH_REDIRECT"):
            break
        page.wait_for_timeout(1000)

    print(f"  final_url : {res['url'][:120]}")
    print(f"  title     : {res['title']}")
    print(f"  n_outward : {len(res['outward']) if res['outward'] else 0}")
    print(f"  n_inward  : {len(res['inward']) if res['inward'] else 0}")

    if verdict == "REAL_FARES":
        o = [r for r in res["outward"] if r["dep"] in ("07:36", "07:38")]
        i = [r for r in res["inward"] if r["dep"] == "18:30"]
        print(f"  target_out : {o}")
        print(f"  target_back: {i}")
        got_o = o[0]["price"] if o else None
        got_i = i[0]["price"] if i else None
        match = (got_o == case["expect_out"] and got_i == case["expect_back"])
        print(f"  expected  : out={case['expect_out']} back={case['expect_back']}")
        print(f"  MATCHES_KNOWN_GOOD: {match}")
    else:
        snippet = re.sub(r"\s+", " ", res["text"] or "")[:600]
        print(f"  body_snippet: {snippet}")

    print(f"VERDICT: {date} {verdict}")
    return verdict


def main() -> int:
    verdicts = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Realistic, honest defaults — a UK user on a normal desktop Chrome.
        # This is not evasion; it is not pretending to be something we aren't
        # beyond a standard browser fingerprint.
        ctx = browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        for case in CASES:
            verdicts.append(probe(page, case))
        browser.close()

    print("\n================ SUMMARY ================")
    for case, v in zip(CASES, verdicts):
        print(f"  {case['date']}: {v}")

    if "CAPTCHA" in verdicts:
        print("VERDICT: OVERALL CAPTCHA — stop. Not building around human verification.")
        return 3
    if all(v == "REAL_FARES" for v in verdicts):
        print("VERDICT: OVERALL REAL_FARES — cloud scrape is viable.")
        return 0
    if "BOT_CHALLENGE" in verdicts:
        print("VERDICT: OVERALL BOT_CHALLENGE — datacentre IP is being blocked.")
        return 4
    print("VERDICT: OVERALL INCONCLUSIVE — see per-date detail above.")
    return 5


if __name__ == "__main__":
    sys.exit(main())
