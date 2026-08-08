// Join yesterday's predictions against what the deployer actually did.
//
//   node scripts/shadow-reconcile.mjs               yesterday (IST)
//   node scripts/shadow-reconcile.mjs 2026-08-08
//
// Reads  shadow/<day>.jsonl.gz        (predictions, written before assignment)
// Writes shadow/<day>.result.jsonl.gz (one compact row per trip, no pool)
//
// The predictions file stays untouched — it is the raw record and the input to
// offline scoring experiments. This produces the small file reports read, so
// answering "how did last week go" never has to decompress megabytes of pools.
//
// THREE THINGS THIS IS CAREFUL ABOUT, each learned by getting it wrong:
//
//   1. Predictions are already pre-assignment by construction — the sweep only
//      logs trips MDS reports as unassigned. Nothing here re-scores anything.
//      Ranking a trip after the fact inflates it badly: the cabs that went to
//      other trips are flagged busy and filtered out, so the deployer's pick
//      floats up a list its real competitors have left.
//
//   2. A trip's VENDOR can change between prediction and assignment. Two trips
//      in a real 02:30 wave swapped vendors, which meant a Layer 1 prediction
//      had been ranking the wrong vendor's cabs entirely — the right answer was
//      never in the pool. That is not a ranking failure and must not be counted
//      as one, so it gets its own bucket.
//
//   3. When several sweeps logged the same trip, the LAST one wins. Later
//      predictions saw more of the wave committed and are what the deployer
//      would have been looking at closest to the decision.

import { readFile, writeFile, access } from 'node:fs/promises';
import { gunzipSync, gzipSync } from 'node:zlib';
import path from 'node:path';

import { trips as mdsTrips, vendorGuids } from '../netlify/functions/_mds.mjs';

const OUT = path.join(process.cwd(), '..', 'shadow');
const iso = (d) => d.toISOString().slice(0, 10);

async function readJsonl(file) {
  const buf = await readFile(file);
  return gunzipSync(buf).toString('utf8').trim().split('\n')
    .filter(Boolean).map((l) => JSON.parse(l));
}

async function main() {
  const istNow = new Date(Date.now() + 5.5 * 3600e3);
  const day = process.argv[2] || iso(new Date(istNow.getTime() - 86400e3));
  const src = path.join(OUT, `${day}.jsonl.gz`);
  try { await access(src); } catch {
    console.log(`no predictions for ${day} — nothing to reconcile`);
    return;
  }

  const preds = await readJsonl(src);
  // last prediction per (buid, trip)
  const latest = new Map();
  for (const p of preds) {
    const k = `${p.buid}|${p.trip_id}`;
    const cur = latest.get(k);
    if (!cur || p.ts > cur.ts) latest.set(k, p);
  }
  console.log(`${day}: ${preds.length} predictions -> ${latest.size} trips`);

  const buids = [...new Set([...latest.values()].map((p) => p.buid))];
  const finals = new Map();     // `${buid}|${tripId}` -> trip
  for (const buid of buids) {
    try {
      const guids = await vendorGuids(buid);
      const raw = await mdsTrips(buid, guids, Date.parse(`${day}T00:00:00+05:30`));
      for (const t of raw) finals.set(`${buid}|${t.tripId}`, t);
      console.log(`   ${buid}: ${raw.length} trips fetched`);
    } catch (e) {
      console.warn(`   ${buid}: FAILED ${String(e.message).slice(0, 140)}`);
    }
  }

  const rows = [];
  const tally = { total: 0, resolved: 0, still_open: 0, vendor_changed: 0, not_found: 0,
                  hit1: 0, hit3: 0, hit5: 0, hit10: 0, not_listed: 0 };

  for (const p of latest.values()) {
    tally.total++;
    const f = finals.get(`${p.buid}|${p.trip_id}`);
    if (!f) { tally.not_found++; continue; }
    if (!f.cabAssigned || !f.cabReg) { tally.still_open++; continue; }

    const chosen = String(f.cabReg).trim();
    const order = p.recs.map((r) => r.cab);
    const rank = order.indexOf(chosen) >= 0 ? order.indexOf(chosen) + 1 : null;
    // old logs carry the MDS pool snapshot; new logs carry the history
    // candidate list. Either way the question is the same: was the deployer's
    // cab even in the set we ranked?
    const inPool = p.candidates ? p.candidates.includes(chosen)
                                : (p.pool || []).some((c) => c[0] === chosen);
    const vendorChanged = String(f.vendorName || '') !== String(p.vendor || '');

    tally.resolved++;
    if (vendorChanged) tally.vendor_changed++;
    if (rank === null) tally.not_listed++;
    else {
      if (rank <= 1) tally.hit1++;
      if (rank <= 3) tally.hit3++;
      if (rank <= 5) tally.hit5++;
      if (rank <= 10) tally.hit10++;
    }

    rows.push({
      day, ts: p.ts, buid: p.buid, trip_id: p.trip_id,
      office: p.office, direction: p.direction, shift: p.shift,
      layer: p.layer,
      vendor_at_prediction: p.vendor,
      vendor_final: f.vendorName || null,
      // A vendor swap makes the prediction unanswerable rather than wrong: we
      // were ranking a pool the answer could not have been in. Reports exclude
      // these from headline accuracy and count them separately.
      vendor_changed: vendorChanged,
      sv_at_prediction: p.subvendor ?? null,
      sv_final: f.subvendor ?? null,
      // if the subvendor changed after we filtered candidates by it, the right
      // cab was structurally outside our list — same class as vendor_changed
      sv_changed: (p.subvendor ?? null) !== null && (f.subvendor ?? null) !== (p.subvendor ?? null),
      chosen, rank,
      // in_pool distinguishes "we ranked it badly" from "MDS never offered it".
      // Only the first is a scoring problem.
      in_pool: inPool,
      eligible: p.eligible, pool_size: p.pool_size,
      history_trips: p.history_trips,
      no_anchor: p.no_anchor, no_capacity: p.no_capacity,
      top1: order[0] || null,
    });
  }

  await writeFile(path.join(OUT, `${day}.result.jsonl.gz`),
                  gzipSync(Buffer.from(rows.map((r) => JSON.stringify(r)).join('\n') + '\n'),
                           { level: 9 }));

  const clean = rows.filter((r) => !r.vendor_changed);
  const pc = (n) => (clean.length ? (100 * n / clean.length).toFixed(1) : '0.0');
  const h = (k) => clean.filter((r) => r.rank !== null && r.rank <= k).length;
  console.log(`\n${day} RESULT`);
  console.log(`   ${tally.resolved} assigned, ${tally.still_open} still open, ${tally.not_found} vanished`);
  console.log(`   ${tally.vendor_changed} changed vendor after we predicted (excluded below)`);
  console.log(`   scored on ${clean.length} trips:`);
  console.log(`     top-1  ${pc(h(1))}%   top-3  ${pc(h(3))}%   top-5  ${pc(h(5))}%   top-10 ${pc(h(10))}%`);
  const missing = clean.filter((r) => r.rank === null);
  const notOffered = missing.filter((r) => !r.in_pool).length;
  console.log(`     ${missing.length} not in our top-10 — of which ${notOffered} were not in the MDS pool at all`);
}

main().catch((e) => { console.error(e); process.exit(1); });
