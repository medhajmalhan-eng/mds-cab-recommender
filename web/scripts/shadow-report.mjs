// What did the recommender actually get right, on live decisions?
//
//   node scripts/shadow-report.mjs            all reconciled days
//   node scripts/shadow-report.mjs --days 7
//
// Reads shadow/*.result.jsonl.gz. This is the number that matters: real waves,
// live pools, every prediction made before the trip was assigned. The backtest
// reconstructs candidates from history and cannot see the feasibility gate or
// busy flags; this can.
//
// Trips whose vendor changed after we predicted are reported separately, not
// folded in. We were ranking a pool the answer could not have been in, so
// counting them as ranking failures would understate the model — and quietly
// hide a real operational fact worth knowing on its own.

import { readdir, readFile } from 'node:fs/promises';
import { gunzipSync } from 'node:zlib';
import path from 'node:path';

const OUT = path.join(process.cwd(), '..', 'shadow');
const arg = (k, d) => {
  const i = process.argv.indexOf(`--${k}`);
  return i > 0 ? process.argv[i + 1] : d;
};

const pct = (n, d) => (d ? (100 * n / d).toFixed(1) + '%' : '—');

function block(label, rows, indent = '') {
  if (!rows.length) return;
  const h = (k) => rows.filter((r) => r.rank !== null && r.rank <= k).length;
  console.log(`${indent}${label.padEnd(30)} n=${String(rows.length).padStart(5)}   ` +
              `top-1 ${pct(h(1), rows.length).padStart(6)}   ` +
              `top-3 ${pct(h(3), rows.length).padStart(6)}   ` +
              `top-5 ${pct(h(5), rows.length).padStart(6)}`);
}

async function main() {
  let files;
  try {
    files = (await readdir(OUT)).filter((f) => f.endsWith('.result.jsonl.gz')).sort();
  } catch {
    console.log('no shadow/ directory yet — run shadow-sweep.mjs, then shadow-reconcile.mjs');
    return;
  }
  if (!files.length) { console.log('nothing reconciled yet'); return; }
  const nDays = +arg('days', 0);
  if (nDays) files = files.slice(-nDays);

  const all = [];
  for (const f of files) {
    const txt = gunzipSync(await readFile(path.join(OUT, f))).toString('utf8');
    for (const l of txt.trim().split('\n')) if (l) all.push(JSON.parse(l));
  }
  if (!all.length) { console.log('no rows'); return; }

  const changed = all.filter((r) => r.vendor_changed);
  const rows = all.filter((r) => !r.vendor_changed);

  console.log(`\nSHADOW REPORT — ${files.length} day(s), ${files[0].slice(0, 10)} .. ${files[files.length - 1].slice(0, 10)}`);
  console.log(`${all.length} assigned trips predicted before assignment\n`);
  block('OVERALL', rows);

  const missing = rows.filter((r) => r.rank === null);
  const notOffered = missing.filter((r) => !r.in_pool).length;
  console.log(`\n  ${missing.length} misses outside the top-10 — ${notOffered} of those were never in ` +
              `the MDS pool (nothing a ranking change can fix), ${missing.length - notOffered} we ranked badly`);
  if (changed.length) {
    console.log(`  ${changed.length} trips changed vendor after we predicted — excluded above. ` +
                `Layer 1 could not have been right on those: we were ranking the wrong vendor's cabs.`);
  }
  const noAnchor = rows.filter((r) => r.no_anchor).length;
  const noCap = rows.filter((r) => r.no_capacity).length;
  if (noAnchor || noCap) {
    console.log(`  data gaps: ${noAnchor} trips had no pickup coordinate, ${noCap} had no stated capacity`);
  }

  const by = (key, label) => {
    const g = new Map();
    for (const r of rows) {
      const k = String(r[key] ?? '—');
      if (!g.has(k)) g.set(k, []);
      g.get(k).push(r);
    }
    console.log(`\nBY ${label}`);
    for (const [k, v] of [...g.entries()].sort((a, b) => b[1].length - a[1].length)) {
      block(k, v, '  ');
    }
  };

  by('layer', 'LAYER');
  by('direction', 'DIRECTION');

  // Shift is where the big variance lives — 09:00 backtested at 54.8% top-1 and
  // 18:30 at 10.7%. An overall average hides that completely, and the whole
  // time-of-day change was motivated by one bad wave at 02:30.
  const byShift = new Map();
  for (const r of rows) {
    if (!byShift.has(r.shift)) byShift.set(r.shift, []);
    byShift.get(r.shift).push(r);
  }
  console.log('\nBY SHIFT (10+ trips only)');
  for (const [k, v] of [...byShift.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    if (v.length >= 10) block(k, v, '  ');
  }

  const byOffice = new Map();
  for (const r of rows) {
    const k = `${r.buid} / ${r.office}`;
    if (!byOffice.has(k)) byOffice.set(k, []);
    byOffice.get(k).push(r);
  }
  console.log('\nBY SITE (10+ trips only)');
  for (const [k, v] of [...byOffice.entries()].sort((a, b) => b[1].length - a[1].length)) {
    if (v.length >= 10) block(k, v, '  ');
  }
  console.log();
}

main().catch((e) => { console.error(e); process.exit(1); });
