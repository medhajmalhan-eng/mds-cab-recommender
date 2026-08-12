// Predict every open trip, before anyone assigns it, and write down what we said.
//
//   node scripts/shadow-sweep.mjs                    rotating slice of the BUs
//   node scripts/shadow-sweep.mjs --buid goc-GocHyd  just one
//
// WHY THIS EXISTS
// ---------------
// Accuracy cannot be measured from the deployed screen until deployers use it,
// and they will not use it until it is trusted. This breaks that deadlock: it
// predicts on its own schedule while deployers keep working in MDS exactly as
// they do today. Nobody has to change anything, and in two weeks there are
// thousands of real decisions to score against instead of the handful we can
// argue about by hand.
//
// It also measures the right thing in a way the offline backtest cannot: the
// feasibility step here runs against each candidate's LIVE schedule (Completed,
// Ongoing and future Planned trips, cross-BU), exactly as the screen does.
//
// THE ONE RULE THAT MAKES THIS HONEST
// -----------------------------------
// Only trips MDS currently reports as unassigned are logged. That is not a
// filter for tidiness — it is what guarantees every prediction predates its own
// answer. Scoring a trip after it has been assigned looks far better than it
// should: the cabs that went elsewhere are flagged busy and filtered out, so the
// competitors vanish and the deployer's pick floats up. That mistake was made
// twice while building this, in both directions, and it is invisible unless you
// are looking for it.
//
// BUDGET
// ------
// A run costs BUs x days x trips x one MDS pool call each. Unbounded across all
// 32 BUs that was ~45 minutes and hit the Actions timeout. Now it is capped at
// 60 predictions, 8 per BU-day, entering the volume-ordered BU list at a
// rotating offset — 77 seconds, spread over ~11 BU-days, and every site reached
// over the course of a day. Measurement wants breadth over time, not everything
// at once.
//
// Env: MDS_EMAIL (or MDS_USERNAME), MDS_PASSWORD
//      SHADOW_BUIDS           optional comma list; default = volume-ordered, thin BUs dropped
//      SHADOW_SAMPLE          trips per BU per day (default 8)
//      SHADOW_MAX_PREDICTIONS ceiling for the whole run (default 60)
//      SHADOW_MIN_HISTORY     skip BUs with less 30-day history than this (default 2000)

import { readFile, readdir, mkdir, appendFile } from 'node:fs/promises';
import { gunzipSync, gzipSync } from 'node:zlib';
import path from 'node:path';

import { trips as mdsTrips, vendorGuids, cabChain } from '../netlify/functions/_mds.mjs';
import {
  History, scorePool, deriveWave, hasVendor, candidatesFor, hhmm,
} from '../public/scorer.js';

const DATA = path.join(process.cwd(), 'data');
const OUT = path.join(process.cwd(), '..', 'shadow');
// Per BU per day. Deliberately small: a run that spends its whole budget on one
// BU measures one BU. Eight apiece spreads a 60-prediction run across seven or
// eight BU-days, which is what makes the per-site breakdown meaningful.
const SAMPLE = +(process.env.SHADOW_SAMPLE || 8);
// Below this much 30-day history a BU cannot be scored meaningfully — most cabs
// have no rows at the office, so everything lands in the "no history" bucket and
// the ranking is deadhead order. Sweeping them wastes budget the real sites need.
const MIN_HISTORY = +(process.env.SHADOW_MIN_HISTORY || 2000);

// A run has to finish inside the Actions timeout, and its cost is
// BUs x days x trips x one MDS pool call each. Sweeping all 32 BUs unbounded
// took ~45s per BU-day and blew past 25 minutes on the first attempt.
//
// So the run is bounded two ways: a hard ceiling on predictions, and BU
// ROTATION. Each run starts at a different offset in the BU list, so over a day
// every BU gets sampled without any single run being enormous. Measurement wants
// breadth over time, not everything at once.
const MAX_PREDICTIONS = +(process.env.SHADOW_MAX_PREDICTIONS || 60);
const POOL_CONCURRENCY = 4;      // parallel vehicle-pool fetches; MDS limits unknown

/** Run async fn over items with bounded concurrency, preserving input order. */
async function mapPool(items, n, fn) {
  const out = new Array(items.length);
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) {
      const k = i++;
      out[k] = await fn(items[k]);
    }
  }));
  return out;
}

const arg = (k, d) => {
  const i = process.argv.indexOf(`--${k}`);
  return i > 0 ? process.argv[i + 1] : d;
};

const iso = (d) => d.toISOString().slice(0, 10);

// How far down the pre-feasibility ranking to check live schedules. Deep enough
// that 10 usually survive; shallow enough that a prediction costs ~15 MDS calls,
// not 150. Candidates below this depth had no realistic chance of the top 5.
const FEAS_DEPTH = 15;

import { feasible } from '../public/scorer.js';

/** The shipped chain check, applied to a history candidate + its live schedule.
 *  The candidate has no busy flag and no nextTrip (those were pool concepts);
 *  the chain IS the source of truth now. */
function feasibleFromChain(rec, trip, chain) {
  return feasible({ busyVehicle: false, nextTrip: null }, trip, chain, false);
}

/** Compact chain encoding for the log: [start_s, end_s, slat, slng, elat, elng,
 *  status0] — enough to replay feasibility offline without re-fetching MDS. */
const encodeChain = (chain) => chain.map((a) => [
  Math.round(a.start), Math.round(a.end),
  a.slat, a.slng, a.elat, a.elng, (a.status || '?')[0],
]);

async function shardFor(buid) {
  const gz = await readFile(path.join(DATA, `${buid}.json.gz`));
  return JSON.parse(gunzipSync(gz).toString('utf8'));
}

/** Spread the sample across waves rather than taking the first N trips.
 *  A run that logs 30 trips from one 09:00 wave tells you about one wave; the
 *  whole point is coverage across shifts, offices and directions. */
function sampleAcrossWaves(open, n) {
  const byWave = new Map();
  for (const t of open) {
    const k = `${t.office}|${t.direction}|${t.shiftTime}`;
    if (!byWave.has(k)) byWave.set(k, []);
    byWave.get(k).push(t);
  }
  const waves = [...byWave.values()];
  const picked = [];
  for (let i = 0; picked.length < n; i++) {
    let progressed = false;
    for (const w of waves) {
      if (i < w.length) { picked.push(w[i]); progressed = true; }
      if (picked.length >= n) break;
    }
    if (!progressed) break;          // every wave exhausted
  }
  return picked;
}

async function sweepBu(buid, day, stamp, budget) {
  const shard = await shardFor(buid);
  const guids = await vendorGuids(buid);
  const dayMs = Date.parse(`${day}T00:00:00+05:30`);
  const raw = await mdsTrips(buid, guids, dayMs);

  const slim = raw.map((t) => ({
    tripId: t.tripId, tripGuid: t.tripGuid, direction: t.tripDirection,
    shiftTime: t.shiftTime, office: t.officeName, vendor: t.vendorName,
    subvendor: t.subvendor || null,
    capacity: t.plannedCabCapacity, cutOff: t.assignmentCutOffTime,
    start: t.tripStartTime, end: t.tripEndTime,
    startGeo: t.tripStartGeoCord, endGeo: t.tripEndGeoCord,
    landmark: t.tripDirection === 'IN' ? t.pickupLandmark : t.dropLandmark,
    assigned: !!t.cabAssigned, cab: t.cabReg,
  }));
  const { assigned, chains } = deriveWave(slim);
  const open = slim.filter((t) => !t.assigned && t.tripGuid);
  if (!open.length) return { buid, open: 0, logged: 0 };

  const picked = sampleAcrossWaves(open, Math.min(SAMPLE, budget));
  const lines = [];
  const histCache = new Map();

  for (const trip of picked) {
    try {
      // THE PIPELINE (rebuilt 2026-08-08): candidates from HISTORY, feasibility
      // from live per-cab schedules, top 5 of what survives.
      //
      // The old pipeline ranked inside MDS's per-trip suggestion list. Deployers
      // ignore that list, and measured on 228 live predictions it omitted a
      // median 25% of the cabs that regularly run the site — one site had 53
      // regulars and the list contained none of them. Live accuracy: 10.5%
      // top-1. Replaying the same decisions with history candidates matched
      // that BEFORE feasibility filtering, and feasibility only removes
      // competitors.
      const cross = !hasVendor(trip);
      const dir = trip.direction === 'IN' ? 'LOGIN' : 'LOGOUT';
      const hk = `${trip.office}|${dir}`;
      if (!histCache.has(hk)) histCache.set(hk, new History(shard, trip.office, dir));
      const hist = histCache.get(hk);
      if (!hist.total) continue;                 // nothing knowable about this site

      const pool = candidatesFor(trip, hist, { crossVendor: cross });
      if (!pool.length) continue;
      const already = new Set(assigned.get(`${trip.shiftTime}|${trip.direction}`) || []);

      // Rank on history first — pure computation, no MDS involved.
      const pre = scorePool(trip, pool, hist, {
        waveAssigned: already, crossVendor: cross, chains, topn: FEAS_DEPTH,
      });

      // Live feasibility, most-promising first: fetch each candidate's actual
      // schedule for the day (all BUs, with geocodes) and run the chain check.
      // Stop once TOPN have survived — a cab we never fetched is reported as
      // unchecked, never assumed free.
      const dayMs = Date.parse(`${day}T00:00:00+05:30`);
      const win = [dayMs - 6 * 3600e3, dayMs + 30 * 3600e3];
      const verdicts = [];                       // {cab, ok, why, chainLen}
      const surviving = [];
      const headCabs = pre.top.map((r) => r.cab);
      const fetchedChains = await mapPool(headCabs, POOL_CONCURRENCY, async (cab) => {
        try { return [cab, await cabChain(cab, win[0], win[1])]; }
        catch (e) { return [cab, null, String(e.message)]; }
      });
      const chainOf = new Map(fetchedChains.map((x) => [x[0], x]));
      for (const r of pre.top) {
        if (surviving.length >= 10) break;
        const [, chain, err] = chainOf.get(r.cab) || [];
        if (chain === null || chain === undefined) {
          verdicts.push({ cab: r.cab, ok: false, why: `schedule fetch failed: ${err || '?'}` });
          continue;
        }
        // drop THIS trip from its own chain if it appears (it is still open, so
        // it should not, but planned data can be ahead of trip/filter)
        const own = chain.filter((a) => String(a.tripId) !== String(trip.tripId));
        const f = feasibleFromChain(r, trip, own);
        verdicts.push({ cab: r.cab, ok: f.ok, why: f.why, chain: own.length });
        if (f.ok) surviving.push({ ...r, feasibility: 'full', chain_trips: own.length });
      }

      const out = { top: surviving.slice(0, 10), eligible: pre.eligible,
                    noAnchor: pre.noAnchor, noCapacity: pre.noCapacity };

      lines.push(JSON.stringify({
        ts: stamp,
        buid, day,
        trip_id: String(trip.tripId),
        office: trip.office, direction: trip.direction,
        shift: hhmm(trip.shiftTime),
        cut_off: hhmm(trip.cutOff),
        landmark: trip.landmark,
        // vendor AS OF PREDICTION TIME. It changes: two trips in a real 02:30
        // wave swapped vendors between prediction and assignment, which makes
        // the prediction unanswerable rather than wrong. Reconcile buckets those
        // separately, and it can only do that if we write this down now.
        vendor: trip.vendor,
        subvendor: trip.subvendor,
        capacity: trip.capacity,
        layer: cross ? 2 : 1,
        pool_size: pool.length,
        eligible: out.eligible,
        no_anchor: out.noAnchor,
        no_capacity: out.noCapacity,
        history_trips: hist.total,
        // WHICH history this prediction saw. The shard is rebuilt every ~3 days
        // (quota limits), so "was this run on fresh data" has to be answerable
        // from the log itself — inferring it from trip counts drifting is not
        // good enough when the answer changes how a result is read.
        history_window: shard.from && shard.to ? `${shard.from}..${shard.to}` : null,
        history_built: shard.built_at || null,
        wave_assigned: [...already],
        recs: out.top.map((r) => ({
          cab: r.cab, tier: r.tier, kernel: +r.kernel.toFixed(6),
          exact: +r.exact_score.toFixed(6), n_exact: r.n_exact,
          n_history: r.n_history, nearest_km: r.nearest_km,
          deadhead_km: r.deadhead_km, feasibility: r.feasibility,
          confidence: r.confidence, vendor: r.vendor,
        })),
        // Candidates are REPRODUCIBLE from history (site+direction+window), so
        // they are not logged — only the regs, for the coverage check. What is
        // NOT reproducible later is the live state: each checked cab's actual
        // schedule at prediction time, and the verdict it got. That is what
        // makes offline replay of feasibility possible without re-fetching MDS.
        candidates: pool.map((c) => c.cabRegNo),
        feas: verdicts,
        chains_checked: Object.fromEntries(
          fetchedChains.filter((x) => x[1]).map(([cab, ch]) => [cab, encodeChain(ch)])),
        trip_attrs: {
          start: trip.start, end: trip.end,
          startGeo: trip.startGeo, endGeo: trip.endGeo,
          shiftTime: trip.shiftTime,
        },
      }));
    } catch (e) {
      console.warn(`   ${buid}/${trip.tripId}: ${String(e.message).slice(0, 100)}`);
    }
  }

  if (lines.length) {
    await mkdir(OUT, { recursive: true });
    // Appending gzip MEMBERS rather than one stream: concatenated members are
    // valid gzip and gunzip reads them as a single document, so each sweep can
    // append without rewriting the day's file.
    await appendFile(path.join(OUT, `${day}.jsonl.gz`),
                     gzipSync(Buffer.from(lines.join('\n') + '\n'), { level: 9 }));
  }
  return { buid, open: open.length, logged: lines.length };
}

async function main() {
  const only = arg('buid') || process.env.SHADOW_BUIDS;
  let buids;
  if (only) {
    buids = only.split(',').map((s) => s.trim()).filter(Boolean);
  } else {
    // Volume order, thin BUs dropped. buids.json is written by the same build
    // that produced the shards, so this can never disagree with what is on disk.
    const idx = JSON.parse(await readFile(path.join(DATA, 'buids.json'), 'utf8'));
    const all = idx.buids || [];
    buids = all.filter((b) => b.trips_30d >= MIN_HISTORY)
               .sort((a, b) => b.trips_30d - a.trips_30d)
               .map((b) => b.buid);
    const dropped = all.length - buids.length;
    if (dropped) console.log(`skipping ${dropped} BU(s) with <${MIN_HISTORY} trips of history`);
  }

  const now = new Date();
  const stamp = now.toISOString();
  // Deployers work the NEXT day's waves through the night — 92% of assignments
  // land within 6h of the trip, and the evening peak is placing tomorrow. Sweep
  // both days or the entire night shift is invisible.
  const istNow = new Date(now.getTime() + 5.5 * 3600e3);
  const today = iso(istNow);
  const tomorrow = iso(new Date(istNow.getTime() + 86400e3));
  const days = istNow.getUTCHours() >= 16 ? [today, tomorrow] : [today];

  // Rotate the starting point so successive runs cover different BUs. With 12
  // runs a day and a 60-prediction ceiling, every BU is reached regularly
  // without any one run trying to do all 32.
  // Already in volume order; rotate the entry point so successive runs start on
  // different sites and every BU is reached over the day.
  const offset = Math.floor(Date.now() / 7200e3) % buids.length;
  buids = [...buids.slice(offset), ...buids.slice(0, offset)];

  console.log(`sweep ${stamp} | ${buids.length} BU(s), starting at ${buids[0]} | ` +
              `days ${days.join(', ')} | cap ${MAX_PREDICTIONS}`);
  let total = 0;
  outer:
  for (const buid of buids) {
    for (const day of days) {
      if (total >= MAX_PREDICTIONS) break outer;
      try {
        const r = await sweepBu(buid, day, stamp, MAX_PREDICTIONS - total);
        if (r.logged) console.log(`   ${buid} ${day}: ${r.logged} logged of ${r.open} open`);
        total += r.logged;
      } catch (e) {
        console.warn(`   ${buid} ${day}: FAILED ${String(e.message).slice(0, 140)}`);
      }
    }
  }
  console.log(`${total} predictions written to shadow/`);
}

main().catch((e) => { console.error(e); process.exit(1); });
