// Replay a live fixture through the ported JS scorer and require it to match
// recommend.py exactly.
//
//   python3 verify_fixture.py ivycomptech-IVYHyd 10 > web/fixture.json
//   node web/scripts/verify.mjs web/fixture.json
//
// public/scorer.js is a hand port of recommend.py. recommend.py is what was
// backtested (41,203 trips, 26.6% top-1); scorer.js is what deployers see. A
// silent divergence — a wrong constant, an off-by-one in the kernel, a sort key
// reversed — would degrade recommendations with no error anywhere and nothing
// on the screen to notice it by.
//
// So: identical inputs, identical outputs. Same cabs, same order, same scores
// to 1e-9, same rejection reasons. Not "close" — identical. Exit 1 otherwise.

import { readFileSync } from 'node:fs';
import { scorePool, History } from '../public/scorer.js';

const EPS = 1e-9;
const path = process.argv[2] || 'fixture.json';
const fx = JSON.parse(readFileSync(path, 'utf8'));

let pass = 0, fail = 0;
const problems = [];

const near = (a, b) => {
  if (a === null || b === null || a === undefined || b === undefined) return a === b || (a ?? null) === (b ?? null);
  return Math.abs(a - b) <= EPS * Math.max(1, Math.abs(a), Math.abs(b));
};

for (const c of fx.cases) {
  const hist = new History(c.bundle, c.office, c.direction);
  const chains = new Map(Object.entries(c.chains));
  const out = scorePool(c.trip, c.pool, hist, {
    waveAssigned: new Set(c.waveAssigned),
    crossVendor: c.cross,
    chains,
    explainRejects: true,
    findCab: c.findCab,
  });

  const errs = [];
  const exp = c.expect;

  if (out.eligible !== exp.eligible) {
    errs.push(`eligible ${out.eligible} != ${exp.eligible}`);
  }

  // Ranking order is the thing that actually reaches the deployer. Compare the
  // full top-5 as an ordered list, not as a set.
  const gotCabs = out.top.map((r) => r.cab);
  const wantCabs = exp.top.map((r) => r.cab);
  if (gotCabs.join('>') !== wantCabs.join('>')) {
    errs.push(`order:\n     js: ${gotCabs.join(' > ')}\n     py: ${wantCabs.join(' > ')}`);
  }

  // Every scored field, not just the order — an order can coincide while the
  // underlying score is wrong, and the next trip would then rank differently.
  for (let i = 0; i < Math.min(out.top.length, exp.top.length); i++) {
    const g = out.top[i], w = exp.top[i];
    for (const k of ['tier', 'exact_score', 'kernel', 'n_exact', 'n_history',
                     'nearest_km', 'fault_rate', 'deadhead_km']) {
      if (!near(g[k], w[k])) errs.push(`#${i + 1} ${g.cab} ${k}: ${g[k]} != ${w[k]}`);
    }
    for (const k of ['no_anchor', 'no_capacity']) {
      if (Boolean(g[k]) !== Boolean(w[k])) errs.push(`#${i + 1} ${g.cab} ${k}: ${g[k]} != ${w[k]}`);
    }
    for (const k of ['feasibility', 'confidence', 'evidence']) {
      if (String(g[k]) !== String(w[k])) errs.push(`#${i + 1} ${g.cab} ${k}: "${g[k]}" != "${w[k]}"`);
    }
  }

  // Rejection reasons matter too: a cab dropped for the wrong reason is a cab
  // that will be wrongly kept as soon as the data shifts slightly.
  const gotRej = new Map(out.rejects.map((r) => [r.cab, r.reason]));
  const wantRej = new Map(exp.rejects.map((r) => [r.cab, r.reason]));
  let rejDiff = 0;
  const rejSamples = [];
  for (const [cab, why] of wantRej) {
    const g = gotRej.get(cab);
    if (g !== why) {
      rejDiff++;
      if (rejSamples.length < 3) rejSamples.push(`${cab}: js="${g ?? '(kept)'}" py="${why}"`);
    }
  }
  for (const cab of gotRej.keys()) {
    if (!wantRej.has(cab)) {
      rejDiff++;
      if (rejSamples.length < 3) rejSamples.push(`${cab}: js="${gotRej.get(cab)}" py="(kept)"`);
    }
  }
  if (rejDiff) errs.push(`${rejDiff} rejection reason(s) differ: ${rejSamples.join(' | ')}`);

  if ((out.found?.rank ?? null) !== (exp.found_rank ?? null)) {
    errs.push(`deployer-cab rank ${out.found?.rank ?? null} != ${exp.found_rank ?? null}`);
  }

  if (errs.length) {
    fail++;
    problems.push({ label: c.label, errs });
    console.log(`FAIL  ${c.label}`);
    for (const e of errs) console.log(`      ${e}`);
  } else {
    pass++;
    console.log(`ok    ${c.label}  (${exp.eligible} eligible, top=${wantCabs[0] || '—'})`);
  }
}

console.log(`\n${pass} passed, ${fail} failed  —  fixture ${fx.buid} ${fx.date}, captured ${fx.generated}`);
if (fail) {
  console.log('\nscorer.js and recommend.py disagree. Do NOT deploy until this is 0:');
  console.log('the backtested accuracy belongs to recommend.py, and only holds for');
  console.log('scorer.js while the two produce the same ranking.');
  process.exit(1);
}
