
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
      let row = dt;
      for (let j = 0; j < 8; j++) {
        if (!row.parentElement) break;
        row = row.parentElement;
        if (row.querySelector('[data-test="train-results-arrival-time"]') && row.querySelector('[data-test="alternative-price"]')) break;
      }
      const depEl = row.querySelector('[data-test="train-results-departure-time"]');
      const arrEl = row.querySelector('[data-test="train-results-arrival-time"]');
      const priceEl = row.querySelector('[data-test="alternative-price"]');
      const depText = depEl ? depEl.textContent : '';
      const arrText = arrEl ? arrEl.textContent : '';
      const depMatch = depText.match(/\d{2}:\d{2}/);
      const arrMatch = arrText.match(/\d{2}:\d{2}/);
      rows.push({ dep: depMatch ? depMatch[0] : null, arr: arrMatch ? arrMatch[0] : null, price: parsePrice(priceEl ? priceEl.textContent.trim() : '') });
    });
    return rows;
  }
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
    if (urlDate !== EXPECTED_DATE) {
      if (Date.now() - start > DEADLINE_MS) return resolve({err: 'wrong_date', got: urlDate});
      return setTimeout(attempt, POLL_MS);
    }
    const out = parseContainer('train-results-container-OUTWARD');
    const ret = parseContainer('train-results-container-INWARD');
    if (!out || !ret || out.length === 0 || ret.length === 0 || isSkeleton(out) || isSkeleton(ret)) {
      if (Date.now() - start > DEADLINE_MS) return resolve({err: 'timeout_or_skeleton', outwardSample: out && out[0], inwardSample: ret && ret[0] });
      return setTimeout(attempt, POLL_MS);
    }
    resolve({outward: out, inward: ret, outwardDate: urlDate});
  }
  attempt();
});
