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
// It also measures the right thing in a way the offline backtest cannot. The
// backtest reconstructs candidates from history; this uses the LIVE MDS pool,
// so the feasibility gate, busy flags and deadheads are all in play.
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

import { trips as mdsTrips, vendorGuids, vehicles } from '../netlify/functions/_mds.mjs';
import {
  History, scorePool, deriveWave, hasVendor, pickProxies, hhmm,
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

// ── pool encoding ───────────────────────────────────────────────────────
// The pool snapshot is what makes offline iteration possible, and it is also
// 95% of the bytes. Stored as objects it was 28.7 KB per prediction — 309 MB a
// month into git, which turns a useful record into a liability. Positional
// arrays with a flag bitmask, then gzip, bring that to roughly 1 KB.
//
// Column order is FIXED. Anything reading these files must use decodeCab below;
// do not index the array by hand at the call site.
export const POOL_COLUMNS = ['reg', 'capacity', 'flags', 'deadhead_m', 'nt_hour', 'nt_min', 'vendor'];
const F_ACTIVE = 1, F_VIRTUAL = 2, F_BUSY = 4, F_COMPLIANT = 8;

const encodeCab = (c) => [
  c.cabRegNo,
  c.capacity ?? -1,
  (c.cabActive ? F_ACTIVE : 0) | (c.virtual ? F_VIRTUAL : 0) |
  (c.busyVehicle ? F_BUSY : 0) | (c.complianceStatus === 'Compliant' ? F_COMPLIANT : 0),
  (c.emptyLegInMetres == null || c.emptyLegInMetres < 0) ? -1 : Math.round(c.emptyLegInMetres),
  c.vehicleNextTripDetails?.hour ?? -1,
  c.vehicleNextTripDetails?.min ?? -1,
  c.__vendor || '',
];

/** Rebuild the shape scorePool() expects. Used by the replay/iteration tooling. */
export function decodeCab(a) {
  const [reg, capacity, flags, dead, nth, ntm, vendor] = a;
  return {
    cabRegNo: reg,
    capacity: capacity === -1 ? null : capacity,
    cabActive: !!(flags & F_ACTIVE),
    virtual: !!(flags & F_VIRTUAL),
    busyVehicle: !!(flags & F_BUSY),
    complianceStatus: (flags & F_COMPLIANT) ? 'Compliant' : 'Non-Compliant',
    emptyLegInMetres: dead === -1 ? null : dead,
    nextTrip: nth === -1 ? null : { hour: nth, min: ntm === -1 ? 0 : ntm },
    __vendor: vendor || null,
  };
}

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

  // Fetch every pool for this BU up front, in parallel. Serially this was ~1.2s
  // per trip and dominated the run; the scoring itself is ~3ms.
  const fetched = await mapPool(picked, POOL_CONCURRENCY, async (trip) => {
    const cross = !hasVendor(trip);
    const proxies = pickProxies(slim, trip, cross);
    const pools = await Promise.all(proxies.map((p) =>
      vehicles(buid, p.guid).then((v) => [p, v]).catch(() => [p, []])));
    return { trip, cross, pools };
  });

  for (const { trip, cross, pools } of fetched) {
    try {
      // Layer follows what the screen would do unprompted: same-vendor when the
      // trip has a vendor, any-vendor when it does not. (Decided during the
      // parallel fetch above, so it travels with the pool.)
      const pool = [];
      for (const [p, cabs] of pools) {
        for (const c of cabs) {
          pool.push({
            cabRegNo: c.cabRegNo, capacity: c.capacity, cabActive: c.cabActive,
            virtual: c.virtual, busyVehicle: c.busyVehicle,
            complianceStatus: c.complianceStatus,
            emptyLegInMetres: c.emptyLegInMetres, subVendorName: c.subVendorName,
            driver: c.drivers?.[0]?.driverName || null,
            vehicleNextTripDetails: c.vehicleNextTripDetails || null,
            nextTrip: c.vehicleNextTripDetails
              ? { hour: c.vehicleNextTripDetails.hour, min: c.vehicleNextTripDetails.min,
                  buid: c.vehicleNextTripDetails.buid }
              : null,
            __vendor: p.vendor,
          });
        }
      }
      if (!pool.length) continue;

      const dir = trip.direction === 'IN' ? 'LOGIN' : 'LOGOUT';
      const hk = `${trip.office}|${dir}`;
      if (!histCache.has(hk)) histCache.set(hk, new History(shard, trip.office, dir));
      const hist = histCache.get(hk);
      const already = new Set(assigned.get(`${trip.shiftTime}|${trip.direction}`) || []);

      const out = scorePool(trip, pool, hist, {
        waveAssigned: already, crossVendor: cross, chains, topn: 10,
      });

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
        capacity: trip.capacity,
        layer: cross ? 2 : 1,
        pool_size: pool.length,
        eligible: out.eligible,
        no_anchor: out.noAnchor,
        no_capacity: out.noCapacity,
        history_trips: hist.total,
        wave_assigned: [...already],
        recs: out.top.map((r) => ({
          cab: r.cab, tier: r.tier, kernel: +r.kernel.toFixed(6),
          exact: +r.exact_score.toFixed(6), n_exact: r.n_exact,
          n_history: r.n_history, nearest_km: r.nearest_km,
          deadhead_km: r.deadhead_km, feasibility: r.feasibility,
          confidence: r.confidence, vendor: r.vendor,
        })),
        // The pool exactly as it was. This is what makes iteration possible
        // WITHOUT a live re-run: any future scoring idea can be replayed against
        // thousands of real decisions offline, with no survivorship, in seconds.
        // Positional — decode with decodeCab(), never by index at the call site.
        pool_cols: POOL_COLUMNS,
        pool: pool.map(encodeCab),
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
