// Trainline results-page extractor.
//
// Usage (from the caller, via mcp__Claude_in_Chrome__javascript_tool):
//   1. Read this file's contents.
//   2. Replace the string `__EXPECTED_DATE__` with the expected outwardDate value
//      from the URL you navigated to (e.g. "2026-06-09T07:00:00" — unencoded,
//      because URLSearchParams.get() returns the decoded value).
//   3. Execute the resulting JS in the tab.
//
// Returns a Promise that resolves to either:
//   {outward: [{dep, arr, price}], inward: [{dep, arr, price}], outwardDate}
// or an error shape:
//   {err: "wrong_date" | "timeout_or_skeleton", got?: "...", outwardSample?, inwardSample?}
//
// Two guards are load-bearing here:
//
//   1. Date guard — Chrome's `navigate` returns before the URL actually updates,
//      so we poll until the query string matches what we asked for.
//   2. Skeleton guard — Trainline renders placeholder rows BEFORE real data
//      arrives: every row comes through as `dep: "02:11", arr: "02:11",
//      price: 88.88`. The old "rows exist" check passes on these ghosts, and
//      the caller ends up validating bogus prices. Added 2026-04-24 after
//      Sophie's nightly run ingested placeholder data on the first few
//      Tuesdays of a batch. Keep polling until the rows look real.
//
// Deadline is 45s because the skeleton state can sit for 10–15s on cold/slow
// loads before real data swaps in.

new Promise(resolve => {
  const EXPECTED_DATE = '__EXPECTED_DATE__';
  const DEADLINE_MS = 45000;
  const POLL_MS = 900;
  const start = Date.now();

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
      // Walk up the DOM until we find an ancestor that has both arrival-time
      // and alternative-price — that's the row element. Max 8 hops is enough
      // for Trainline's current nesting; bail early if we find it sooner.
      let row = dt;
      for (let j = 0; j < 8; j++) {
        if (!row.parentElement) break;
        row = row.parentElement;
        if (
          row.querySelector('[data-test="train-results-arrival-time"]') &&
          row.querySelector('[data-test="alternative-price"]')
        ) break;
      }
      const depEl = row.querySelector('[data-test="train-results-departure-time"]');
      const arrEl = row.querySelector('[data-test="train-results-arrival-time"]');
      const priceEl = row.querySelector('[data-test="alternative-price"]');
      const depText = depEl ? depEl.textContent : '';
      const arrText = arrEl ? arrEl.textContent : '';
      const depMatch = depText.match(/\d{2}:\d{2}/);
      const arrMatch = arrText.match(/\d{2}:\d{2}/);
      rows.push({
        dep: depMatch ? depMatch[0] : null,
        arr: arrMatch ? arrMatch[0] : null,
        price: parsePrice(priceEl ? priceEl.textContent.trim() : ''),
      });
    });
    return rows;
  }

  // Skeleton detector. Real Trainline result sets have distinct departure times
  // across rows (different trains leave at different times). The placeholder
  // state uses identical values for every row. Any of these triggers another
  // poll:
  //   - all rows share the same `dep` (placeholder uses 02:11 for every row)
  //   - every row's price is exactly £88.88 (Trainline's skeleton price)
  //   - every row has `dep === arr` (nonsense for a real journey)
  function isSkeleton(rows) {
    if (!rows || !rows.length) return true;
    const deps = new Set(rows.map(r => r.dep));
    if (deps.size < 2) return true;
    if (rows.every(r => r.price === 88.88)) return true;
    if (rows.every(r => r.dep === r.arr)) return true;
    return false;
  }

  function attempt() {
    const params = new URL(window.location.href).searchParams;
    const urlDate = params.get('outwardDate');

    // Date guard: if the URL hasn't caught up to what we navigated to, keep
    // polling. This catches Chrome's "navigate returns early" race.
    if (urlDate !== EXPECTED_DATE) {
      if (Date.now() - start > DEADLINE_MS) {
        return resolve({err: 'wrong_date', got: urlDate});
      }
      return setTimeout(attempt, POLL_MS);
    }

    const out = parseContainer('train-results-container-OUTWARD');
    const ret = parseContainer('train-results-container-INWARD');

    if (!out || !ret || out.length === 0 || ret.length === 0 || isSkeleton(out) || isSkeleton(ret)) {
      if (Date.now() - start > DEADLINE_MS) {
        return resolve({
          err: 'timeout_or_skeleton',
          outwardSample: out && out[0],
          inwardSample: ret && ret[0],
        });
      }
      return setTimeout(attempt, POLL_MS);
    }

    resolve({outward: out, inward: ret, outwardDate: urlDate});
  }

  attempt();
});
