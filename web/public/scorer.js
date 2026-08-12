// Scoring engine — a direct port of ../../recommend.py.
//
// The spec below was frozen after backtesting 41,203 trips across 6 Hyderabad
// BU-offices (rolling 30d, no leakage). Measured: 26.6% top-1 / 45.5% top-3 /
// 55.2% top-5 against what the deployer actually did.
//
//   hard filters -> exact-route tier -> fallback kernel -> reliability tiebreak
//
// Things that were tested and made it WORSE, so don't re-add them:
//   - employee<->cab affinity (<1pt)
//   - windows past 30 days (top-1 flat; only +1pt top-5)
//   - greedy global assignment (20.4% vs 24.2% — cabs legitimately do 3-5 trips/day)
//   - reliability as a ranking term rather than a tiebreak (49.3% -> 49.1%)
//   - the garage anchor applied globally (helps LOGIN-first only)
//
// IF YOU CHANGE A CONSTANT HERE, change it in recommend.py too and re-run
// backtest.py. The Python side is what proved these numbers; this file is what
// deployers actually see. Silent drift between them is the failure mode this
// repo layout exists to prevent — verify.mjs asserts the two agree.
//
// Trips are the TRIMMED shape emitted by netlify/functions/wave.mjs, not raw
// MDS objects: {tripId, tripGuid, direction, shiftTime, office, vendor,
// capacity, cutOff, start, end, startGeo, endGeo, landmark, assigned, cab}.

// ── scoring constants (all measured, do not tune casually) ──
export const KERNEL_KM = 3.0;      // exp(-d/KERNEL_KM)
export const KERNEL_CAP_KM = 10.0; // beyond this, no credit. 3km was too tight: cost 2.7pts top-5
export const EXACT_KM = 1.0;       // "same route" radius for the dominant tier
export const SHIFT_BONUS = 2.0;    // multiplier is (1 + SHIFT_BONUS * shift_similarity)
export const RECENCY_HALFLIFE_D = 21.0;
export const SPECIFICITY = 0.5;    // divide by n_cab**this. Rewards cabs that ONLY do this area
export const FEASIBILITY_BUFFER_MIN = 30;

// ── time-of-day terms (added 2026-08-08) ────────────────────────────────
// Measured with ../../experiment.py. Held out on 6,708 trips from a period the
// tuning never saw: top-1 20.8 -> 23.7, top-5 47.0 -> 52.6. Confirmed on 17,174
// trips overall (+2.2 / +5.4). Neither works alone — separately they are +0.2
// and +0.5, which is noise; together they are worth ~5pts of top-5.
//
// Why: shift matching used to be exact STRING equality, so a shift with little
// history had the x3 bonus — the strongest term — permanently switched off. A
// real 02:30 wave at Ivy had 8 historical trips at that time out of 12,498 at
// the site: nothing scored `strong`, every card read "no exact match", and
// ranking collapsed onto daytime area familiarity while the deployer picked
// night regulars. Rare shifts go 41.5 -> 52.1 top-5.
export const TOD_TAU = 30.0;       // minutes; swept 15..180, flat 30-45, worse past 90
export const TIER_DELTA_MIN = 0;   // tier 1 still requires an EXACT match — untested otherwise
export const DUTY_W = 16.0;        // weight on "works this hour at all", no distance filter
export const DUTY_TAU = 60.0;      // minutes; 60 beat 120 and 240
// SOFT TIER — TESTED AND REJECTED (2026-08-08): multiplicative blend instead of
// the lexicographic exact-route tier won on the backtest (+2 top-1, held out
// too) but lost the PAIRED test on 146 live decisions, 1 gain vs 4 losses in
// top-5. Do not re-add without a live paired win.

// A cab this far from the pickup cannot serve it. Vendor pools are city-wide —
// Ivy has a Pune office, so a Hyderabad trip's pool contains MH-plated Pune cabs
// ~490 km away. The chain check only catches those that happen to have another
// trip that day; an idle one has no chain and would sail through.
export const MAX_DEADHEAD_KM = 60;
// Coarse version of the same gate for CROSS-VENDOR pools. There the deadhead was
// computed against a proxy trip of that vendor (same office preferred), so the
// number is approximate — but out-of-city cabs read ~480 km, so a loose gate
// still removes them without false rejects.
export const MAX_DEADHEAD_XV_KM = 120;

const KM_PER_DEG_LAT = 111.0;
const KM_PER_DEG_LNG = 105.9;      // at Hyderabad's latitude (~17.4 N)

export const ROAD_FACTOR = 1.4;    // straight-line km -> road km, Hyderabad
export const AVG_KMH = 20.0;       // MEASURED, not guessed: median planned_km /
                                   // planned duration across 24,903 real Ivy
                                   // trips = 20.2 km/h. This was 25 and that made
                                   // the feasibility check ~24% optimistic — it
                                   // accepted chains a cab could not make. p25 is
                                   // 17 km/h, so 20 is central, not conservative.

export const km = (alat, alng, blat, blng) =>
  Math.hypot((alat - blat) * KM_PER_DEG_LAT, (alng - blng) * KM_PER_DEG_LNG);

export const travelMin = (d) => (d * ROAD_FACTOR) / AVG_KMH * 60.0;

// ── time ────────────────────────────────────────────────────────────────
// Everything clock-related is pinned to IST rather than the browser's locale.
// Python's datetime.fromtimestamp() ran on an IST server; a browser opened from
// any other timezone would shift every shift string by hours, so `shift ==
// tshift` would never match and the x3 SHIFT_BONUS would silently vanish —
// degrading recommendations with no error anywhere.
const IST_OFFSET_MS = 5.5 * 3600e3;
const istDate = (ms) => new Date(ms + IST_OFFSET_MS);
export const hhmm = (ms) => {
  const d = istDate(ms);
  return String(d.getUTCHours()).padStart(2, '0') + ':' +
         String(d.getUTCMinutes()).padStart(2, '0');
};
const minOfDay = (sec) => {
  const d = istDate(sec * 1000);
  return d.getUTCHours() * 60 + d.getUTCMinutes();
};

/** '02:30' -> 150. null when unparseable — the extract carries the literal
 *  string 'null' for a missing shift, and treating that as a time would match
 *  it against everything. */
export function shiftMins(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm ?? '').trim());
  return m ? +m[1] * 60 + +m[2] : null;
}

/** Minutes between two times of day, the short way round the clock. 23:45 and
 *  00:15 are 30 minutes apart, not 1410 — and night waves, which is where this
 *  whole term earns its keep, sit right on that boundary. */
export function circDelta(a, b) {
  const d = Math.abs(a - b) % 1440;
  return Math.min(d, 1440 - d);
}

/** Render a missing value as "?" rather than the language's own spelling. These
 *  strings are shown to deployers AND compared against recommend.py — "null" vs
 *  "None" would be a permanent false failure in the verification harness. */
const nz = (x) => (x === null || x === undefined) ? '?' : String(x);

export function parseGeo(s) {
  if (!s) return [null, null];
  const [a, b] = String(s).split(',').map(Number);
  return Number.isFinite(a) && Number.isFinite(b) ? [a, b] : [null, null];
}

// ── history ─────────────────────────────────────────────────────────────
// Wraps one BU shard (public/data/<buid>.json) with the lookups the scorer
// needs. Equivalent to the History class in recommend.py, minus SQLite.
export class History {
  constructor(bundle, office, direction) {
    this.office = office;
    this.direction = direction;
    this.built_at = bundle.built_at;
    this.from = bundle.from;
    this.to = bundle.to;
    const cabs = bundle.cabs || [];
    // Shard rows are [cabIdx, lat, lng, shift, age]; append parsed shift minutes
    // once here rather than re-parsing 12k strings on every trip click. Done in
    // the constructor, not the build, so existing shards keep working.
    const rows = (bundle.shards?.[`${office}|${direction}`] || [])
      .map((r) => (r.length > 5 ? r : [r[0], r[1], r[2], r[3], r[4], shiftMins(r[3])]));
    this.rows = rows;
    this.byCab = new Map();
    for (const r of rows) {
      const cab = cabs[r[0]];
      let a = this.byCab.get(cab);
      if (!a) { a = []; this.byCab.set(cab, a); }
      a.push(r);
    }
    // faults are office-scoped (both directions), matching cab_profiles' key of
    // (cab_reg, bunit_id, office) — a driver's reliability is not per-direction.
    this.faults = new Map();
    for (const [ci, [n, rate]] of Object.entries(bundle.faults?.[office] || {})) {
      this.faults.set(cabs[ci], { n, fault: rate });
    }
    // cab -> {vendor, capacity}: the cab's MASTER vendor (MDS's own vocabulary)
    // and seat count, from its most recent trip at this BU. This is what lets
    // candidates come from history instead of MDS's suggestion list.
    this.meta = new Map();
    for (const [ci, m] of Object.entries(bundle.cab_meta || {})) {
      const [vendor, capacity, subvendor, caps, svs] = m;
      this.meta.set(cabs[ci], {
        vendor, capacity, subvendor: subvendor || null,
        // Every capacity/subvendor seen in the window. Older (format 2) shards
        // carry only the latest, so fall back to that rather than filtering
        // everything out on a stale shard.
        caps: caps && caps.length ? caps : (capacity ? [capacity] : []),
        svs: svs && svs.length ? svs : (subvendor ? [subvendor] : []),
      });
    }
  }
  get total() { return this.rows.length; }
}

// ── candidates ──────────────────────────────────────────────────────────
/**
 * The candidate pool, built from HISTORY — every cab that has worked this
 * site+direction in the window — instead of MDS's per-trip suggestion list.
 *
 * Why: measured on 228 live predictions, MDS's list omitted a median 25% of the
 * cabs that regularly run the site (79 of 228 predictions were missing over
 * HALF of the regulars; one site had 53 regulars and the list contained none of
 * them). Deployers ignore that list; ranking inside it capped us at whatever it
 * happened to contain.
 *
 * Vendor filter is on the MASTER vendor, case-folded: 'MIS-One' and 'MIS-ONE'
 * both occur in the extract. Subvendor names must NOT be used here — one master
 * spans several subvendors, and matching on subvendor excluded the deployer's
 * actual pick in 58 of 102 replayed decisions.
 *
 * Everything here is knowable BEFORE assignment. Feasibility (is the cab free
 * right now) is a separate, later step against live schedules — see feasible().
 */
export function candidatesFor(trip, hist, { crossVendor = false } = {}) {
  const wantVendor = String(trip.vendor || '').trim().toLowerCase();
  const wantSv = String(trip.subvendor || '').trim();
  const wantCap = trip.capacity;
  const out = [];
  for (const [cab, m] of hist.meta) {
    if (!hist.byCab.has(cab)) continue;          // meta exists but not at this office+dir
    // Match against ANY capacity/subvendor the cab has worked in the window,
    // not just its most recent. Cabs move between subvendors and get
    // reclassified; using only the latest wrongly excluded the deployer's own
    // pick in 19 of 744 live decisions. Paired live test: +2 top-5, 0 losses.
    if (wantCap && m.caps.length && !m.caps.includes(wantCap)) continue;
    if (!crossVendor) {
      if (wantSv) {
        // The trip already names its SUBVENDOR — the deployer's own workflow is
        // subvendor first, cab second (live waves show open trips carrying it).
        // Same vocabulary on both sides, so this is an exact match.
        // Backtested: +0.9 top-1 / +2.0 top-5 over the master-vendor filter.
        if (!m.svs.some((v) => String(v).trim() === wantSv)) continue;
      } else if (trip.vendor) {
        if (String(m.vendor || '').trim().toLowerCase() !== wantVendor) continue;
      }
    }
    out.push({
      cabRegNo: cab,
      capacity: wantCap || m.capacity,
      cabActive: true, virtual: false, busyVehicle: false,
      complianceStatus: 'Compliant',      // unknown from history; flagged, not faked — see UI
      emptyLegInMetres: null,             // no live GPS; deadhead comes from the chain check
      subVendorName: null,
      __vendor: m.vendor || null,
    });
  }
  return out;
}

// ── feasibility ─────────────────────────────────────────────────────────
/**
 * Can this cab actually take this trip, given what it's already committed to?
 *
 * Two levels, because MDS gives two different qualities of information:
 *
 * FULL (spatial) — when `chain` has the cab's other assignments for the day,
 * each with real geocodes from trip/filter. Then we check both legs:
 *     prev trip END  -> this trip START   must fit in the gap
 *     this trip END  -> next trip START   must fit in the gap
 * Travel time is straight-line x ROAD_FACTOR / AVG_KMH.
 *
 * TIME-ONLY (fallback) — nextTrip carries {hour, min, buid} with NO location
 * and no date, so for commitments outside the BUs we can see, only the clock can
 * be checked. Those are flagged so the caller knows the check was weaker, rather
 * than silently passing.
 *
 * CAVEAT on the fallback: it is undocumented whether hour/min is that trip's
 * start or end. Read as a start time — the conservative choice for a NEXT trip,
 * which is the one that can actually be blocked.
 *
 * `isOwn`: this cab is the one already deployed on THIS trip (evaluation mode).
 * MDS flags it busy *because* of this trip, and points its "next trip" at this
 * trip — so both signals must be ignored, or the very cab we're trying to score
 * is always rejected.
 */
export function feasible(cab, trip, chain = null, isOwn = false) {
  if (cab.busyVehicle && !isOwn) return { ok: false, why: 'busy', depth: 'full' };

  const tStart = trip.start / 1000;
  const tEnd = trip.end / 1000;
  const [tSlat, tSlng] = parseGeo(trip.startGeo);
  const [tElat, tElng] = parseGeo(trip.endGeo);
  const buf = FEASIBILITY_BUFFER_MIN * 60;

  // ---------- FULL: real chain with geocodes ----------
  if (chain && chain.length) {
    for (const a of chain) {
      if (a.end > tStart - buf && a.start < tEnd + buf) {
        return { ok: false, why: `overlaps its ${a.label || 'other'} trip`, depth: 'full' };
      }
    }
    let prev = null;
    for (const a of chain) if (a.end <= tStart && (!prev || a.end > prev.end)) prev = a;
    if (prev && prev.elat !== null && tSlat !== null) {
      const need = travelMin(km(prev.elat, prev.elng, tSlat, tSlng)) * 60;
      if (prev.end + need > tStart) {
        return {
          ok: false,
          why: `can't reach pickup from its ${prev.label || 'previous'} trip ` +
               `(needs ${Math.round(need / 60)} min, has ${Math.round((tStart - prev.end) / 60)})`,
          depth: 'full',
        };
      }
    }
    let nxt = null;
    for (const a of chain) if (a.start >= tEnd && (!nxt || a.start < nxt.start)) nxt = a;
    if (nxt && nxt.slat !== null && tElat !== null) {
      const need = travelMin(km(tElat, tElng, nxt.slat, nxt.slng)) * 60;
      if (tEnd + need > nxt.start) {
        return {
          ok: false,
          why: `can't reach its next ${nxt.label || ''} trip ` +
               `(needs ${Math.round(need / 60)} min, has ${Math.round((nxt.start - tEnd) / 60)})`,
          depth: 'full',
        };
      }
    }
  }

  // ---------- TIME-ONLY: commitments we can only see the clock for ----------
  const nd = cab.nextTrip;
  if (nd && nd.hour !== null && nd.hour !== undefined && !isOwn) {
    const ndMin = nd.hour * 60 + (nd.min || 0);
    // nextTrip has NO date. If its time-of-day is before this trip even starts,
    // it is almost certainly tomorrow's trip — comparing it to today's clock
    // would false-reject every cab whose next duty is an early-morning login,
    // which is exactly the set a midnight deployer is placing.
    if (ndMin >= minOfDay(tStart)) {
      if (ndMin < minOfDay(tEnd) + FEASIBILITY_BUFFER_MIN) {
        return {
          ok: false,
          why: `next trip ${String(nd.hour).padStart(2, '0')}:${String(nd.min || 0).padStart(2, '0')} (${nd.buid || '?'})`,
          depth: 'time-only',
        };
      }
    }
  }
  return { ok: true, why: null, depth: (chain && chain.length) ? 'full' : 'time-only' };
}

// ── scoring ─────────────────────────────────────────────────────────────
/**
 * Rank a live MDS candidate pool against local history.
 *
 * `crossVendor` only changes how deadhead is reported: MDS computes
 * emptyLegInMetres for the trip that was queried, so when the pool came from a
 * different trip (the only way to see another vendor's cabs) it is wrong and
 * must be suppressed rather than shown.
 */
export function scorePool(trip, pool, hist, {
  waveAssigned = new Set(), crossVendor = false, topn = 5,
  chains = new Map(), explainRejects = false, findCab = null,
} = {}) {
  const direction = trip.direction === 'IN' ? 'LOGIN' : 'LOGOUT';
  const [tlat, tlng] = parseGeo(direction === 'LOGIN' ? trip.startGeo : trip.endGeo);
  // ~0.7% of live trips carry no anchor coordinate at all (13 of 1,973 measured
  // across three BUs, some of them unassigned). Every distance would come out
  // NaN, every comparison would be false, and the list would still render —
  // silently ordered by nothing. Detect it and say so instead.
  const noAnchor = tlat === null;
  // Some trips carry no capacity. Filtering on it then rejected the ENTIRE pool
  // — 190 of 190 cabs at goc-GocHyd, and 1.6% of live trips are like this — and
  // the deployer got an empty list with no reason given. With no capacity stated
  // there is no constraint to enforce, so enforce none and say so.
  const wantCap = trip.capacity;
  const noCap = !wantCap;
  const tshift = hhmm(trip.shiftTime);
  const tshiftM = shiftMins(tshift);
  const hl = RECENCY_HALFLIFE_D;

  const out = [], rejects = [], seen = new Set();
  for (const c of pool) {
    const cab = c.cabRegNo;
    if (!cab || seen.has(cab)) continue;    // Layer 2 unions pools; a cab can repeat
    seen.add(cab);
    const drop = (reason) => { if (explainRejects) rejects.push({ cab, reason }); };

    // the deployer already used this cab elsewhere in this wave
    if (waveAssigned.has(cab)) { drop('already deployed in this wave'); continue; }
    if (!c.cabActive || c.virtual) { drop('inactive/virtual'); continue; }
    if (c.complianceStatus !== 'Compliant') { drop('non-compliant'); continue; }
    // "?" rather than the language's own null spelling, so this string is
    // identical in scorer.js and recommend.py and verify.mjs can compare it.
    if (!noCap && c.capacity !== wantCap) {
      drop(`capacity ${nz(c.capacity)} != ${nz(wantCap)}`); continue;
    }

    // Deadhead gate. Exact on the trip's own pool; coarse (wider limit) on a
    // cross-vendor pool, where MDS computed emptyLegInMetres against a proxy
    // trip of that vendor. Without the coarse gate an IDLE out-of-city cab —
    // no chain, so nothing else rejects it — could surface in Layer 2.
    const el = c.emptyLegInMetres;
    const lim = crossVendor ? MAX_DEADHEAD_XV_KM : MAX_DEADHEAD_KM;
    if (el !== null && el !== undefined && el >= 0 && el / 1000 > lim) {
      drop(`${Math.round(el / 1000)} km away (limit ${lim})`); continue;
    }

    const f = feasible(c, trip, chains.get(cab), cab === findCab);
    if (!f.ok) { drop(f.why); continue; }

    const rows = hist.byCab.get(cab) || [];
    let nExact = 0, exactW = 0, kern = 0, duty = 0, nearest = null;
    // Without a coordinate there is no route to match on, so every history term
    // stays zero and the ranking falls through to deadhead.
    for (const [, lat, lng, shift, age, shm] of (noAnchor ? [] : rows)) {
      const r = Math.pow(0.5, age / hl);
      // Duty accumulates BEFORE the distance gate, on purpose: "does this cab
      // work this hour" is a fact about the cab, not about this pickup.
      // Filtering it by distance would collapse it back into the kernel.
      if (shm !== null && tshiftM !== null) {
        duty += Math.exp(-circDelta(shm, tshiftM) / DUTY_TAU) * r;
      }
      const d = km(tlat, tlng, lat, lng);
      if (nearest === null || d < nearest) nearest = d;
      if (d > KERNEL_CAP_KM) continue;
      const sim = (shm === null || tshiftM === null)
        ? 0 : Math.exp(-circDelta(shm, tshiftM) / TOD_TAU);
      kern += Math.exp(-d / KERNEL_KM) * (1 + SHIFT_BONUS * sim) * r;
      if (d <= EXACT_KM && shm !== null && tshiftM !== null
          && circDelta(shm, tshiftM) <= TIER_DELTA_MIN) { nExact += 1; exactW += r; }
    }
    const nRows = Math.max(rows.length, 1);
    kern /= Math.pow(nRows, SPECIFICITY);
    kern *= (1 + DUTY_W * (duty / nRows));      // specificity first, then duty

    const prof = hist.faults.get(cab);
    out.push({
      cab,
      subvendor: c.subVendorName || null,
      driver: c.driver || null,
      tier: nExact > 0 ? 1 : 2,
      exact_score: exactW,
      kernel: kern,
      n_exact: nExact,
      n_history: rows.length,
      // Rounded ONCE, at the precision the evidence string displays. Rounding to
      // 2 dp and then formatting at 1 dp manufactured exact .x5 values, which
      // Python (half-to-even) and JS (half-up) resolve differently — a permanent
      // mismatch in verify.mjs on roughly one row in ten.
      nearest_km: nearest === null ? null : Math.round(nearest * 10) / 10,
      fault_rate: prof ? prof.fault : 0,
      // "full" = chain checked with real geocodes; "time-only" = clock only,
      // because that commitment sits in a BU we can't see trips for
      feasibility: f.depth,
      // suppressed for cross-vendor: the number belongs to a different trip
      deadhead_km: (el !== null && el !== undefined && el >= 0 && !crossVendor)
        ? Math.round(el / 100) / 10 : null,
      no_anchor: noAnchor,
      no_capacity: noCap,
      vendor: c.__vendor || null,
    });
  }

  if (noAnchor) {
    // Nothing to rank on but proximity. Cabs with no deadhead figure sort last
    // rather than first — an unknown distance is not a short one.
    out.sort((a, b) =>
      ((a.deadhead_km ?? 9e9) - (b.deadhead_km ?? 9e9)) ||
      (a.fault_rate - b.fault_rate));
  } else {
    out.sort((a, b) =>
      (a.tier - b.tier) ||
      (b.exact_score - a.exact_score) ||
      (b.kernel - a.kernel) ||
      (a.fault_rate - b.fault_rate));
  }

  for (const r of out) {
    r.evidence = evidence(r);
    r.confidence = confidenceOf(r, noAnchor);
  }

  // For an already-deployed trip: where did the cab the deployer actually chose
  // land in our ranking? Lets a whole finished wave be checked at a glance.
  let found = null;
  if (findCab) {
    const i = out.findIndex((r) => r.cab === findCab);
    if (i >= 0) found = { ...out[i], rank: i + 1 };
    else {
      const inPool = pool.some((c) => c.cabRegNo === findCab);
      found = {
        cab: findCab, rank: null, in_pool: inPool,
        why: rejects.find((x) => x.cab === findCab)?.reason ||
             (inPool ? 'filtered out — re-run with debug for the reason'
                     : 'not offered by MDS for this trip'),
      };
    }
  }

  return { top: out.slice(0, topn), eligible: out.length, all: out, rejects, found,
           noAnchor, noCapacity: noCap };
}

// Calibrated against 549 live pre-assignment decisions (2026-08-08..10): the
// deployer's pick reached our top-5 24% of the time when the #1 card had NO
// exact-route history, ~30% at 1-3, and 48% at 4+. The old rule called any cab
// with one exact trip "strong" — 417 of 549 cards, predicting 34.8%, barely
// above "medium" at 25%. A label that fires on three quarters of cards tells a
// deployer nothing. Re-derive as the shadow log grows.
export const STRONG_EXACT = 4;
export const MEDIUM_EXACT = 1;

export function confidenceOf(r, noAnchor = false) {
  if (noAnchor) return 'weak';
  if (r.n_exact >= STRONG_EXACT) return 'strong';
  if (r.n_exact >= MEDIUM_EXACT || r.n_history) return 'medium';
  return 'weak';
}

export function evidence(r) {
  let s;
  if (r.no_anchor) {
    s = 'MDS has no pickup coordinate for this trip — cannot match on route';
    if (r.deadhead_km !== null) s += ` · nearest available, ${r.deadhead_km.toFixed(1)} km away`;
    if (r.fault_rate >= 0.10) s += ` · ⚠ driver-fault ${Math.round(100 * r.fault_rate)}%`;
    return s;
  }
  if (r.n_exact) {
    s = `did this route ${r.n_exact}× in 30d (nearest ${r.nearest_km.toFixed(1)} km)`;
  } else if (r.n_history) {
    s = `no exact match; ${r.n_history} trips in this area, nearest ${r.nearest_km.toFixed(1)} km`;
  } else {
    s = 'no history at this site';
  }
  if (r.deadhead_km !== null) s += ` · ${r.deadhead_km.toFixed(1)} km away now`;
  if (r.fault_rate >= 0.10) s += ` · ⚠ driver-fault ${Math.round(100 * r.fault_rate)}%`;
  return s;
}

// ── wave state ──────────────────────────────────────────────────────────
/**
 * Derive assignment state from a trip list. Rebuilt from scratch on every poll,
 * never accumulated: a monotonic "already used" set would keep a cab hidden
 * after the deployer un-assigns it.
 *
 * Two things come out of the same data:
 *   assigned — cabs already holding a trip in this (shift, direction).
 *              Validated at 99.87%: a cab does not take two trips in one wave.
 *   chains   — every assigned trip today per cab, WITH geocodes, so feasibility
 *              can check "can it physically get from its last drop to this
 *              pickup, and on to its next pickup" rather than just clock times.
 */
export function deriveWave(trips) {
  const byId = new Map(), assigned = new Map(), chains = new Map();
  for (const t of trips) {
    byId.set(String(t.tripId), t);
    if (!t.cab) continue;
    const wk = `${t.shiftTime}|${t.direction}`;
    let s = assigned.get(wk);
    if (!s) { s = new Set(); assigned.set(wk, s); }
    s.add(t.cab);

    const [slat, slng] = parseGeo(t.startGeo);
    const [elat, elng] = parseGeo(t.endGeo);
    let c = chains.get(t.cab);
    if (!c) { c = []; chains.set(t.cab, c); }
    c.push({
      start: t.start / 1000, end: t.end / 1000,
      slat, slng, elat, elng,
      label: `${t.direction} ${hhmm(t.shiftTime)}`,
      tripId: t.tripId,
    });
  }
  for (const c of chains.values()) c.sort((a, b) => a.start - b.start);
  return { byId, assigned, chains };
}

/** One entry per WAVE — (direction, shift, office) — not per shift time. MDS
 * lists 'IN 19:30' and 'OUT 19:30' separately because they are different waves,
 * and collapsing them hides half the picture. Includes fully-deployed waves so
 * the shift list matches what the deployer sees in MDS. */
export function shiftSummary(trips) {
  const agg = new Map();
  for (const t of trips) {
    const k = `${t.direction}|${hhmm(t.shiftTime)}|${t.office}`;
    let a = agg.get(k);
    if (!a) { a = { total: 0, open: 0 }; agg.set(k, a); }
    a.total++;
    if (!t.assigned) a.open++;
  }
  return [...agg.entries()]
    .map(([k, v]) => {
      const [direction, shift, office] = k.split('|');
      return { direction, shift, office, ...v };
    })
    .sort((a, b) => a.shift.localeCompare(b.shift) ||
                    a.direction.localeCompare(b.direction) ||
                    a.office.localeCompare(b.office));
}

const NO_VENDOR = ['', 'none', 'not assigned', 'na', 'null'];
export const hasVendor = (t) =>
  !NO_VENDOR.includes(String(t.vendor || '').trim().toLowerCase());

/**
 * Which trips to fetch candidate pools from.
 *
 * Layer 1 -> the trip's own pool (MDS scopes it to the trip's vendor).
 * Layer 2 -> one pool per distinct vendor in the wave, unioned. There is no
 * vendor parameter on the endpoint, so another vendor's cabs are only visible
 * via one of that vendor's own trips.
 *
 * The proxy trip is chosen from the SAME OFFICE as the target when possible:
 * emptyLegInMetres is computed against the proxy, so a proxy at another office
 * (Ivy has IN-PUNE!) would make every deadhead — and the coarse cross-vendor
 * deadhead gate — nonsense.
 */
export function pickProxies(trips, trip, cross) {
  if (!cross) return [{ vendor: trip.vendor, guid: trip.tripGuid }];
  const wantOffice = trip?.office;
  const proxies = new Map();
  for (const t of trips) {
    const v = t.vendor;
    const cur = proxies.get(v);
    if (!cur || (wantOffice && t.office === wantOffice && cur.office !== wantOffice)) {
      proxies.set(v, t);
    }
  }
  return [...proxies.entries()].map(([vendor, t]) => ({ vendor, guid: t.tripGuid }));
}
