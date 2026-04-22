// Reference copy of the Trainline DOM extractor — the CANONICAL version is
// embedded inline in RUNBOOK.md (step 3) so the scheduled-task prompt is
// self-contained and doesn't depend on this file being loaded first.
// Keep this file and the RUNBOOK snippet in sync if either changes.
//
// Called via chrome mcp javascript_tool. Waits for render, then pulls OUT + RETURN rows.
// Note: the newer daily_run.py validator requires exact-match 07:36 OUT and
// 18:30 RETURN. The time-window logic in this older version (OUT_MAX/RET_MIN/
// RET_MAX) captures more rows than needed — the raw output is fine, but
// validation in daily_run.py only accepts the exact matches.
new Promise(resolve => {
  const start = Date.now();
  const OUT_MAX = 7 * 60 + 36;
  const RET_MIN = 18 * 60 + 30;
  const RET_MAX = 20 * 60 + 30;

  function toMin(s) {
    const m = s && s.match(/(\d{2}):(\d{2})/);
    return m ? parseInt(m[1]) * 60 + parseInt(m[2]) : null;
  }
  function parsePrice(s) {
    if (!s) return null;
    const m = s.match(/£\s*([\d,]+(?:\.\d+)?)/);
    return m ? parseFloat(m[1].replace(/,/g, '')) : null;
  }
  function parseContainer(testId) {
    const container = document.querySelector(`[data-test="${testId}"]`);
    if (!container) return null;
    const depTimes = container.querySelectorAll('[data-test="train-results-departure-time"]');
    const rows = [];
    depTimes.forEach((dt) => {
      let row = dt;
      for (let j = 0; j < 8; j++) {
        if (!row.parentElement) break;
        row = row.parentElement;
        if (row.querySelector('[data-test="train-results-arrival-time"]') &&
            row.querySelector('[data-test="alternative-price"]')) break;
      }
      const dep = (row.querySelector('[data-test="train-results-departure-time"]')||{}).textContent || '';
      const arr = (row.querySelector('[data-test="train-results-arrival-time"]')||{}).textContent || '';
      const altPrice = row.querySelector('[data-test="alternative-price"]');
      const cheapestLabel = row.querySelector('[data-test="cheapest-price-label"]');
      const depMin = toMin(dep);
      const arrMin = toMin(arr);
      const priceText = altPrice ? altPrice.textContent.trim() : '';
      const price = parsePrice(priceText);
      const limitedAvail = /Limited availability|Only \d+ left/i.test(row.textContent);
      const onlyNLeft = (row.textContent.match(/Only (\d+) left/i) || [])[1] || null;
      rows.push({
        dep_time: dep.match(/\d{2}:\d{2}/) ? dep.match(/\d{2}:\d{2}/)[0] : null,
        arr_time: arr.match(/\d{2}:\d{2}/) ? arr.match(/\d{2}:\d{2}/)[0] : null,
        dep_min: depMin,
        arr_min: arrMin,
        fare: price,
        is_cheapest: !!cheapestLabel,
        limited_availability: limitedAvail,
        only_n_left: onlyNLeft ? parseInt(onlyNLeft) : null,
      });
    });
    return rows;
  }

  function attempt() {
    const out = parseContainer('train-results-container-OUTWARD');
    const ret = parseContainer('train-results-container-INWARD');
    if (!out || !ret || out.length === 0 || ret.length === 0) {
      if (Date.now() - start > 25000) {
        return resolve({err: 'timeout', outLen: out ? out.length : -1, retLen: ret ? ret.length : -1});
      }
      return setTimeout(attempt, 700);
    }
    // Filter rows to sensible same-day dep times (exclude overnight >= 00:00 return edge cases noise)
    const outInWindow = out.filter(r => r.dep_min !== null && r.dep_min <= OUT_MAX);
    const retInWindow = ret.filter(r => r.dep_min !== null && r.dep_min >= RET_MIN && r.dep_min <= RET_MAX);

    function pickCheapest(arr) {
      const withFare = arr.filter(r => r.fare !== null);
      if (!withFare.length) return null;
      return withFare.reduce((a, b) => (b.fare < a.fare ? b : a));
    }
    const outBest = pickCheapest(outInWindow);
    const retBest = pickCheapest(retInWindow);

    resolve({
      checked_at: new Date().toISOString(),
      url: window.location.href,
      outbound_in_window: outInWindow,
      return_in_window: retInWindow,
      outbound_all: out,
      return_all: ret,
      best_out: outBest,
      best_ret: retBest,
      total_cheapest: (outBest && retBest) ? +(outBest.fare + retBest.fare).toFixed(2) : null,
    });
  }
  attempt();
});
