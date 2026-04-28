#!/usr/bin/env python3
"""Long-term fare dataset + movement analysis + pattern detection.

Why this file exists:
  The `history` array on each Tuesday in prices.json caps at 30 entries and
  only stores the combined "cheapest_any_total". That's fine for yesterday vs
  today, but it is NOT enough to:
    (a) distinguish a leg-level price step (outward vs return) from a both-
        legs-moved event,
    (b) detect bulk pricing releases (same delta hitting many dates at once),
    (c) spot new historical lows across runs,
    (d) learn the Advance fare ladder (the discrete price tiers Trainline
        cycles through as inventory burns),
    (e) eventually model "when do prices typically drop" for this route.

  This module keeps an append-only JSONL log of every observation we've ever
  made, plus analytical helpers that turn that log into the signals Sophie
  sees on the tracker.

Files:
  fare_history.jsonl — append-only, one line per (Tuesday × observation).
  Never edit in place. If schema needs to change, add a version field and
  handle old rows in the reader.

Schema (v2):
  {
    "schema": 2,                                # bumped from 1 when we added
                                                #   twox_advance_premium
    "observed_at":  ISO8601 UTC timestamp,     # when the scrape happened
    "observed_on":  YYYY-MM-DD,                # UK date (for grouping)
    "travel_date":  YYYY-MM-DD,                # the Tuesday the fare is for
    "days_out":     int,                       # travel_date − observed_on
    "weeks_out":    int,                       # weeks until travel
    "out_07_36":    float,                     # 07:36 outward fare (SplitSave)
    "back_18_30":   float,                     # 18:30 return fare (Advance Single)
    "total":        float,                     # sum of the two Sophie-valid rows
    "twox_advance_premium":  float | None,     # v2: the extra £ Sophie pays to
                                                #   swap SplitSave for 2x Advance
                                                #   Singles on the outward. Scraped
                                                #   from /book/ticket-options on the
                                                #   nightly run. null for v1 rows
                                                #   captured before this field existed.
    "cheapest_out": {"dep": "HH:MM", "fare": float},   # cheapest outward any time
    "cheapest_in":  {"dep": "HH:MM", "fare": float},   # cheapest return any time
    "run_id":       ISO timestamp              # == observed_at, for joining
  }

Schema evolution notes:
  - Readers MUST accept v1 rows with `twox_advance_premium` absent — treat
    as None. Analysis code should gracefully ignore null premiums.
  - Back-fill for historical v1 rows is NOT feasible (Trainline prices
    move, so re-scraping today gives today's data not the original day's).
    The field just stays null for pre-Phase-4 rows.
"""
from __future__ import annotations

import json
from datetime import datetime, date, timedelta
from pathlib import Path
from statistics import median
from typing import Iterable

ROOT = Path(__file__).parent
HISTORY = ROOT / "fare_history.jsonl"

# Sophie's fixed constraints
OUT_DEP = "07:36"
BACK_DEP = "18:30"


# ────────────────────────────────────────────────────────────────────────────
# Append-only writer
# ────────────────────────────────────────────────────────────────────────────

def _pick(rows: list[dict], dep: str) -> dict | None:
    for r in rows or []:
        if (r.get("dep") or "").strip() == dep:
            return r
    return None


def _cheapest(rows: list[dict]) -> dict | None:
    """Cheapest row in a leg, ignoring rows with null prices."""
    best = None
    for r in rows or []:
        p = r.get("price")
        if not isinstance(p, (int, float)):
            continue
        if best is None or p < best.get("price", 1e9):
            best = r
    if not best:
        return None
    return {"dep": best.get("dep"), "fare": best.get("price")}


def observations_from_snapshot(raw_snapshot: dict) -> list[dict]:
    """Build a list of fare_history rows from raw_snapshot.json content.

    Used both live (after a successful run) and by the backfill script to
    ingest historical snapshots pulled from git."""
    observed_at = raw_snapshot.get("probed_at") or (
        datetime.utcnow().isoformat(timespec="seconds") + "Z"
    )
    try:
        observed_on = datetime.strptime(observed_at[:10], "%Y-%m-%d").date()
    except ValueError:
        observed_on = date.today()

    rows: list[dict] = []
    for t in raw_snapshot.get("tuesdays") or []:
        dstr = t.get("date")
        if not dstr:
            continue
        try:
            travel = datetime.strptime(dstr, "%Y-%m-%d").date()
        except ValueError:
            continue
        out_row = _pick(t.get("outward") or [], OUT_DEP)
        back_row = _pick(t.get("inward") or [], BACK_DEP)
        out_fare = out_row.get("price") if out_row else None
        back_fare = back_row.get("price") if back_row else None
        if not isinstance(out_fare, (int, float)) or not isinstance(back_fare, (int, float)):
            # Skip rows where Sophie's constraints weren't met — those aren't
            # legitimate data points for trend analysis anyway.
            continue
        # twox_advance_premium may or may not be present depending on whether
        # the scheduled task captured the ticket-options page. Pass through
        # as-is; readers accept null.
        premium = t.get("twox_advance_premium")
        if not isinstance(premium, (int, float)):
            premium = None
        rows.append({
            "schema": 2,
            "observed_at": observed_at,
            "observed_on": observed_on.strftime("%Y-%m-%d"),
            "travel_date": dstr,
            "days_out": (travel - observed_on).days,
            "weeks_out": max(0, ((travel - observed_on).days + 3) // 7),
            "out_07_36": round(float(out_fare), 2),
            "back_18_30": round(float(back_fare), 2),
            "total": round(float(out_fare) + float(back_fare), 2),
            "twox_advance_premium": (round(premium, 2) if premium is not None else None),
            "cheapest_out": _cheapest(t.get("outward") or []),
            "cheapest_in": _cheapest(t.get("inward") or []),
            "run_id": observed_at,
        })
    return rows


def append_observations(rows: Iterable[dict], path: Path = HISTORY) -> int:
    """Append rows to fare_history.jsonl, returning the count actually written.

    Idempotency note: we do NOT deduplicate at write time — the backfill
    script is responsible for not re-inserting rows with run_ids already
    present. For live daily runs every run_id is unique so duplicates
    can't happen."""
    n = 0
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
            n += 1
    return n


def load_history(path: Path = HISTORY) -> list[dict]:
    """Read every row from fare_history.jsonl. Silently skips unparseable
    lines so a single bad write can't poison downstream analysis."""
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def existing_run_ids(path: Path = HISTORY) -> set[str]:
    """For backfill idempotency — which (run_id, travel_date) pairs are
    already in the log."""
    seen: set[str] = set()
    for r in load_history(path):
        key = f"{r.get('run_id')}|{r.get('travel_date')}"
        seen.add(key)
    return seen


# ────────────────────────────────────────────────────────────────────────────
# Movement analysis — runs in daily_run.py after apply_fresh_prices
# ────────────────────────────────────────────────────────────────────────────

def _prior_legs(prior_current: dict | None) -> tuple[float | None, float | None]:
    """Return (prior_out_fare, prior_back_fare) from a Tuesday's `current`
    snapshot from BEFORE the current run mutated it."""
    if not prior_current:
        return None, None
    out = prior_current.get("out") or {}
    back = prior_current.get("back") or {}
    of = out.get("fare") if isinstance(out.get("fare"), (int, float)) else None
    bf = back.get("fare") if isinstance(back.get("fare"), (int, float)) else None
    return of, bf


def _prior_from_history(history: list[dict]) -> dict[str, dict]:
    """Second-most-recent observation per travel_date, used as the 'prior'
    baseline for movement analysis. Using fare_history (append-only, immune
    to double-runs) is more robust than reading the in-memory `current`
    block — a re-run of the same day overwrites in-memory state, but the
    log preserves every distinct observation."""
    by_travel = _observations_by_travel(history)
    out: dict[str, dict] = {}
    for td, rows in by_travel.items():
        if len(rows) < 2:
            continue
        prev = rows[-2]
        # Shape it to match the `current` schema that analyse_movements reads.
        out[td] = {
            "out": {"fare": prev.get("out_07_36")},
            "back": {"fare": prev.get("back_18_30")},
            "cheapest_any_total": prev.get("total"),
        }
    return out


def analyse_movements(
    prices: dict,
    prior_by_date: dict[str, dict],
    history: list[dict],
    bulk_min_count: int = 3,
) -> dict:
    """Group today's price changes into bulk events + outliers, identify new
    historical lows.

    Arguments:
      prices         — the mutated prices.json dict (current = today's scrape)
      prior_by_date  — {date: prior_current_dict}. Intentionally IGNORED if
                       empty/stale; we prefer fare_history as the prior source
                       because a same-day re-run erases in-memory prior state
                       but not the append-only log. Passed in for callers who
                       really do want to force a comparison against an
                       explicit snapshot — otherwise leave it {}.
      history        — fare_history.jsonl rows INCLUDING today's just-appended
                       observations
      bulk_min_count — minimum dates moving by identical (leg, delta) to count
                       as a bulk event (default 3 — two isn't a pattern)

    Returns a dict shaped for direct storage in prices["last_run"]["movements"]:
      {
        "bulk_events": [
          {"leg": "outward"|"inward",
           "delta": float, "from": float, "to": float,
           "count": int, "dates": [YYYY-MM-DD, ...]}
        ],
        "outliers": [
          {"date": ..., "leg": "outward"|"inward"|"both",
           "delta_out": float, "delta_back": float,
           "reason": "single-date move / different baseline"}
        ],
        "new_lows": [
          {"date": ..., "total": float, "prior_low": float,
           "observations": int}   # how many prior observations this low beats
        ],
        "unchanged_count": int,
        "any_movement": bool
      }
    """
    # Prefer fare_history as the source for "prior" — append-only, immune
    # to same-day re-runs that would overwrite the in-memory `current`.
    # Only fall back to prior_by_date if we genuinely don't have a prior
    # observation in the log (first ever run for this Tuesday).
    history_priors = _prior_from_history(history)

    movements_per_tuesday: list[dict] = []
    for t in prices.get("tuesdays") or []:
        if t.get("booked"):
            continue
        dstr = t["date"]
        cur = t.get("current") or {}
        new_total = cur.get("cheapest_any_total")
        new_out = ((cur.get("out") or {}).get("fare"))
        new_back = ((cur.get("back") or {}).get("fare"))
        prior = history_priors.get(dstr) or prior_by_date.get(dstr) or {}
        prior_out, prior_back = _prior_legs(prior)
        prior_total = prior.get("cheapest_any_total")
        if not all(isinstance(v, (int, float)) for v in (new_total, new_out, new_back)):
            continue
        d_out = round(new_out - prior_out, 2) if isinstance(prior_out, (int, float)) else None
        d_back = round(new_back - prior_back, 2) if isinstance(prior_back, (int, float)) else None
        d_total = round(new_total - prior_total, 2) if isinstance(prior_total, (int, float)) else None
        movements_per_tuesday.append({
            "date": dstr,
            "prior_out": prior_out,
            "prior_back": prior_back,
            "prior_total": prior_total,
            "new_out": new_out,
            "new_back": new_back,
            "new_total": new_total,
            "d_out": d_out,
            "d_back": d_back,
            "d_total": d_total,
        })

    # Bucket by (leg, delta) to find bulk events. Only meaningful when |Δ| ≥ £0.01.
    buckets: dict[tuple[str, float], list[dict]] = {}
    for m in movements_per_tuesday:
        if m["d_out"] is not None and abs(m["d_out"]) >= 0.01:
            buckets.setdefault(("outward", m["d_out"]), []).append(m)
        if m["d_back"] is not None and abs(m["d_back"]) >= 0.01:
            buckets.setdefault(("inward", m["d_back"]), []).append(m)

    bulk_events: list[dict] = []
    dates_in_bulk: set[tuple[str, str]] = set()  # (leg, date) — for outlier detection
    for (leg, delta), members in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        if len(members) < bulk_min_count:
            continue
        # Pick the modal `from` fare as the canonical baseline — handles the
        # case where most dates move £93.70→£86.70 but one moves £84.10→£77.10
        # (same delta, different starting point; split those into two groups).
        from_counts: dict[float, list[dict]] = {}
        for m in members:
            fare = m["prior_out" if leg == "outward" else "prior_back"]
            from_counts.setdefault(fare, []).append(m)
        for from_fare, group in from_counts.items():
            if len(group) < bulk_min_count:
                continue
            to_fare = round(from_fare + delta, 2)
            bulk_events.append({
                "leg": leg,
                "delta": delta,
                "from": from_fare,
                "to": to_fare,
                "count": len(group),
                "dates": sorted(m["date"] for m in group),
            })
            for m in group:
                dates_in_bulk.add((leg, m["date"]))

    # Outliers: any date where the leg moved but isn't in a bulk event.
    outliers: list[dict] = []
    for m in movements_per_tuesday:
        out_moved = m["d_out"] is not None and abs(m["d_out"]) >= 0.01
        back_moved = m["d_back"] is not None and abs(m["d_back"]) >= 0.01
        out_in_bulk = ("outward", m["date"]) in dates_in_bulk
        back_in_bulk = ("inward", m["date"]) in dates_in_bulk
        tags = []
        if out_moved and not out_in_bulk:
            tags.append("outward")
        if back_moved and not back_in_bulk:
            tags.append("inward")
        if not tags:
            continue
        if len(tags) == 2:
            leg = "both"
        else:
            leg = tags[0]
        outliers.append({
            "date": m["date"],
            "leg": leg,
            "delta_out": m["d_out"],
            "delta_back": m["d_back"],
            "delta_total": m["d_total"],
            "from_out": m["prior_out"],
            "to_out": m["new_out"],
            "from_back": m["prior_back"],
            "to_back": m["new_back"],
        })

    # New historical lows — use fare_history, EXCLUDE today's observations to
    # get the prior-low baseline.
    today_str = date.today().strftime("%Y-%m-%d")
    by_travel_prior: dict[str, list[float]] = {}
    for r in history:
        if r.get("observed_on") == today_str:
            continue
        td = r.get("travel_date")
        tot = r.get("total")
        if td and isinstance(tot, (int, float)):
            by_travel_prior.setdefault(td, []).append(tot)

    new_lows: list[dict] = []
    for m in movements_per_tuesday:
        prior_totals = by_travel_prior.get(m["date"], [])
        if not prior_totals:
            continue  # first observation — can't claim a "new" low
        prior_low = min(prior_totals)
        if m["new_total"] < prior_low - 0.01:
            new_lows.append({
                "date": m["date"],
                "total": m["new_total"],
                "prior_low": round(prior_low, 2),
                "beats_by": round(prior_low - m["new_total"], 2),
                "observations": len(prior_totals),
            })

    unchanged = sum(
        1 for m in movements_per_tuesday
        if (m["d_total"] is not None and abs(m["d_total"]) < 0.01)
    )
    any_movement = bool(bulk_events or outliers or new_lows)

    # Per-Tuesday move record — so the site can look up "did THIS date
    # move, and if so, which leg" for each card's pill.
    per_tuesday = {m["date"]: {
        "d_out": m["d_out"], "d_back": m["d_back"], "d_total": m["d_total"],
        "prior_out": m["prior_out"], "prior_back": m["prior_back"],
        "new_out": m["new_out"], "new_back": m["new_back"],
    } for m in movements_per_tuesday}

    return {
        "bulk_events": bulk_events,
        "outliers": outliers,
        "new_lows": new_lows,
        "per_tuesday": per_tuesday,
        "unchanged_count": unchanged,
        "any_movement": any_movement,
        "analysed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


# ────────────────────────────────────────────────────────────────────────────
# Pattern detection — derives "rules" from fare_history.jsonl
# ────────────────────────────────────────────────────────────────────────────

def _observations_by_travel(history: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in history:
        td = r.get("travel_date")
        if td:
            out.setdefault(td, []).append(r)
    for td in out:
        out[td].sort(key=lambda r: r.get("observed_at") or "")
    return out


def compute_patterns(history: list[dict], prices: dict) -> dict:
    """Extract trend rules for display. Safe on empty history — returns an
    empty-ish dict rather than crashing, so the site can cope with a fresh
    deployment before any runs have happened."""
    if not history:
        return {
            "route_min": None, "route_median": None, "route_max": None,
            "fare_ladder_out_07_36": [], "fare_ladder_back_18_30": [],
            "all_time_low_by_tuesday": {},
            "bulk_events_last_30d": [],
            "observations_total": 0,
            "observations_last_30d": 0,
            "first_observation": None,
            "last_updated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }

    totals = [r["total"] for r in history if isinstance(r.get("total"), (int, float))]
    out_fares = [r["out_07_36"] for r in history if isinstance(r.get("out_07_36"), (int, float))]
    back_fares = [r["back_18_30"] for r in history if isinstance(r.get("back_18_30"), (int, float))]

    # Fare ladders = observed discrete price points (rounded to nearest 10p,
    # deduped). That's the empirical "Advance tier ladder" Trainline cycles.
    def _ladder(fares: list[float]) -> list[float]:
        if not fares:
            return []
        uniq = sorted({round(f, 2) for f in fares})
        return uniq[:20]  # cap display; real ladders are usually 4–8 rungs

    # All-time low per Tuesday
    by_travel = _observations_by_travel(history)
    lows = {td: round(min(r["total"] for r in rows if isinstance(r.get("total"), (int, float))), 2)
            for td, rows in by_travel.items()
            if any(isinstance(r.get("total"), (int, float)) for r in rows)}

    # Per-date max-min spread, then take the median across dates. This is the
    # honest "how much does the price for a SINGLE date typically move?" number.
    # Crucially: NOT the same as route_max - route_min, which mixes apples (a
    # cheap October date) with oranges (an expensive June date) and overstates
    # how much any individual date actually swings. Without this Sophie sees
    # huge "spread" numbers and thinks fares are about to drop, when really
    # different dates just sit at different price points.
    per_date_spreads = []
    for td, rows in by_travel.items():
        date_totals = [r["total"] for r in rows if isinstance(r.get("total"), (int, float))]
        if len(date_totals) >= 2:
            per_date_spreads.append(round(max(date_totals) - min(date_totals), 2))
    median_per_date_spread = round(median(per_date_spreads), 2) if per_date_spreads else None
    pct_at_floor = None  # % of observations sitting at the per-date all-time-low
    if lows and history:
        floor_hits = 0
        for r in history:
            t = r.get("total")
            if isinstance(t, (int, float)) and r.get("travel_date") in lows:
                if abs(t - lows[r["travel_date"]]) < 0.5:
                    floor_hits += 1
        if history:
            pct_at_floor = round(100 * floor_hits / len(history), 1)

    # Recent activity window (30 days)
    cutoff = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
    recent = [r for r in history if (r.get("observed_on") or "") >= cutoff]

    # Detect bulk events across the last 30 days by scanning day-over-day
    # deltas on the 07:36 outward — the leg that drives most of the variance.
    bulk_events_30d = _detect_bulk_events_in_history(recent, leg_key="out_07_36", min_count=3)

    first = min((r.get("observed_on") or "" for r in history if r.get("observed_on")), default=None)

    return {
        "route_min": round(min(totals), 2) if totals else None,
        "route_median": round(median(totals), 2) if totals else None,
        "route_max": round(max(totals), 2) if totals else None,
        "median_per_date_spread": median_per_date_spread,
        "pct_at_floor": pct_at_floor,
        "fare_ladder_out_07_36": _ladder(out_fares),
        "fare_ladder_back_18_30": _ladder(back_fares),
        "all_time_low_by_tuesday": lows,
        "bulk_events_last_30d": bulk_events_30d,
        "observations_total": len(history),
        "observations_last_30d": len(recent),
        "first_observation": first,
        "last_updated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


def _detect_bulk_events_in_history(
    rows: list[dict], leg_key: str, min_count: int = 3
) -> list[dict]:
    """Scan a window of observations for day-over-day bulk moves on a leg.

    For each pair of adjacent observed_on values per travel_date, compute the
    leg delta; then group by (observed_on, delta) and emit any bucket ≥
    min_count. Limits output to 10 events for display sanity."""
    by_travel = _observations_by_travel(rows)
    # (observed_on → (delta → [(travel_date, from, to)]))
    events: dict[str, dict[float, list[tuple]]] = {}
    for td, seq in by_travel.items():
        for prev, cur in zip(seq, seq[1:]):
            pf = prev.get(leg_key)
            cf = cur.get(leg_key)
            if not isinstance(pf, (int, float)) or not isinstance(cf, (int, float)):
                continue
            delta = round(cf - pf, 2)
            if abs(delta) < 0.01:
                continue
            key = cur.get("observed_on") or ""
            events.setdefault(key, {}).setdefault(delta, []).append((td, pf, cf))

    flat: list[dict] = []
    for observed_on, by_delta in events.items():
        for delta, members in by_delta.items():
            if len(members) < min_count:
                continue
            flat.append({
                "observed_on": observed_on,
                "leg": "outward" if leg_key == "out_07_36" else "inward",
                "delta": delta,
                "affected_count": len(members),
                "sample_dates": sorted(td for td, _, _ in members)[:5],
            })
    flat.sort(key=lambda e: (e["observed_on"], -e["affected_count"]))
    return flat[-10:]  # most recent 10
