// RUNBOOK step 4 — 2x-Advance-Single premium capture, hardened.
//
// Usage (via mcp__claude-in-chrome__javascript_tool, on a /book/results page
// that has already been scraped by the trainline-lookup extractor):
//
//   await eval(sessionStorage.__premium)('07:36', '18:30', 43.8, 27.0)
//
// The last two args are the ALREADY-VALIDATED leg prices from the scrape.
// They are what makes this safe: the basket is only trusted if it agrees
// with them.
//
// Returns {twox, standard, opts} on success, or {err, ...} — never throws.
// Step 4 is non-blocking: any {err} is recorded as null and the run continues.
//
// ── Why this file exists ────────────────────────────────────────────────────
// This logic used to live only as prose in RUNBOOK step 4, re-derived by hand
// on every nightly run. On 2026-08-20 the hand-written version did
// `if (!el.checked) { el.click(); await sleep(700); }` and then immediately
// clicked Continue. Trainline priced its default selection — the CHEAPEST
// rows — instead of Sophie's, producing a £54 basket (08:23 out £27 + 20:30
// back £27) that reached her message as "cheapest unbooked". Her real total
// was £70.80.
//
// Root cause, verified against a live page on 2026-08-21: the element IS a
// real <input> and `.checked` reads correctly, so the original "`.checked` was
// undefined" theory was wrong. The click fired but the selection had not
// propagated into the basket before Continue was clicked — 700ms was not
// enough. The failure was invisible because a wrong basket looks exactly like
// a cheap one.
//
// Two guards now prevent it:
//   1. Selection is verified AFTER clicking (re-read, retry, then bail) with a
//      900ms settle. We never click Continue on an unconfirmed selection.
//   2. The basket total must equal out + back. This is the real backstop —
//      Trainline pricing a different journey is indistinguishable from a
//      genuine discount unless you check the arithmetic.
//
// Guard 2 is duplicated server-side in daily_run.reconcile_basket(), so a
// future regression here still cannot publish a phantom price.
//
// A null is recoverable. A plausible wrong number is not.

sessionStorage.__premium = `(async function(OUTT, INT, OUT_FARE, BACK_FARE){
  const sl = ms => new Promise(r => setTimeout(r, ms));

  // Resolve the actual radio <input> for a given departure time. The element
  // carrying the test id may be a wrapper, so walk into it for an input.
  function radioFor(containerId, depTime){
    const c = document.querySelector('[data-test="' + containerId + '"]');
    if (!c) return null;
    for (const dt of c.querySelectorAll('[data-test="train-results-departure-time"]')) {
      if (!dt.textContent.includes(depTime)) continue;
      let row = dt;
      for (let j = 0; j < 9; j++) {
        if (!row.parentElement) break;
        row = row.parentElement;
        const hit = row.querySelector('[data-test="standard-class-price-radio-btn"]');
        if (hit) {
          const input = hit.matches('input[type=radio]')
            ? hit
            : hit.querySelector('input[type=radio]');
          return { clickTarget: hit, input: input || null };
        }
      }
    }
    return null;
  }

  function isSelected(h){
    if (!h) return false;
    if (h.input) return !!h.input.checked;
    // No real input found — fall back to ARIA, and treat unknown as NOT
    // selected so we always click rather than assuming.
    const a = h.clickTarget.getAttribute('aria-checked');
    return a === 'true';
  }

  async function ensureSelected(h, label){
    if (isSelected(h)) return true;
    (h.input || h.clickTarget).click();
    await sl(900);
    if (isSelected(h)) return true;
    h.clickTarget.click();          // second attempt on the wrapper
    await sl(900);
    return isSelected(h);
  }

  try {
    // Hydration gate. Wait for the ACTUAL target radios to exist — not just
    // for rows to appear. Trainline's skeleton rows carry departure-time nodes
    // but no radio, so a row-count gate returns too early and radioFor comes
    // back null. Observed 2026-08-21: the first call after every navigate
    // failed with radio_not_found and only succeeded on a manual retry.
    let o = null, i = null;
    const hydrateBy = Date.now() + 45000;
    while (Date.now() < hydrateBy) {
      o = radioFor('train-results-container-OUTWARD', OUTT);
      i = radioFor('train-results-container-INWARD', INT);
      if (o && i) break;
      await sl(900);
    }
    if (!o || !i) return { err: 'radio_not_found', out: !!o, back: !!i };

    if (!await ensureSelected(o, 'outward')) return { err: 'outward_select_failed' };
    if (!await ensureSelected(i, 'inward'))  return { err: 'inward_select_failed' };

    const btn = document.querySelector('[data-test="cjs-button-continue"]');
    if (!btn) return { err: 'no_continue' };
    btn.click();

    let dl = Date.now() + 15000;
    while (Date.now() < dl) {
      if (location.href.includes('/book/ticket-options')) break;
      await sl(500);
    }
    if (!location.href.includes('/book/ticket-options')) return { err: 'no_ticket_options' };

    // Gate on '+£' AND '2x Single Tickets'. The heading is 'Select Flexibility'
    // (not 'Ticket type'), and 'SplitSave' renders before its prices do — so
    // gating on either of those parses an empty set.
    let dl2 = Date.now() + 15000, txt = '';
    while (Date.now() < dl2) {
      txt = document.body.innerText;
      if (/\\+£[\\d.]+/.test(txt) && /2x Single Tickets/.test(txt)) break;
      await sl(600);
    }
    if (!(/\\+£[\\d.]+/.test(txt) && /2x Single Tickets/.test(txt))) {
      return { err: 'options_not_ready' };
    }

    const L = txt.split('\\n').map(s => s.trim());
    const opts = {};
    for (let k = 1; k < L.length; k++) {
      const m = L[k].match(/^\\+£([\\d.]+)$/);
      if (m) opts[L[k - 1]] = parseFloat(m[1]);
    }
    let standard = null;
    for (let k = 0; k < L.length - 1; k++) {
      if (L[k] === 'Standard') {
        const mm = L[k + 1].match(/£([\\d.]+)/);
        if (mm) { standard = parseFloat(mm[1]); break; }
      }
    }

    // THE load-bearing check. If the basket doesn't price the trains we asked
    // for, everything read off this page describes the wrong journey.
    const expected = Math.round((OUT_FARE + BACK_FARE) * 100) / 100;
    if (standard === null) return { err: 'no_standard_total', opts };
    if (Math.round(standard * 100) / 100 !== expected) {
      return { err: 'basket_mismatch', standard, expected, opts };
    }

    return {
      twox: opts['2x Single Tickets'] !== undefined ? opts['2x Single Tickets'] : null,
      standard,
      opts
    };
  } catch (e) {
    return { err: 'exception', msg: String(e) };
  }
})`;
"premium_capture cached in sessionStorage.__premium";
