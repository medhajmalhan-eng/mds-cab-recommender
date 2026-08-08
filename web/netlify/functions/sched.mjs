// POST /api/sched   {"cabs": ["TG-08-V-2063", ...], "date": "YYYY-MM-DD"}
//   -> { chains: { "TG-08-V-2063": [{start,end,slat,slng,elat,elng,label,status}, ...] } }
//
// Live schedules for NAMED cabs — Completed, Ongoing and future Planned trips,
// across all BUs, with geocodes. The browser ranks candidates from history
// (which needs no server at all) and then calls this for its top candidates so
// feasible() can check each one against what the cab is actually committed to.
//
// This replaced /api/pool as the only live-MDS step in a recommendation. The
// old pool was also the CANDIDATE source, which was wrong twice over: deployers
// ignore MDS's suggestions, and the list omitted a median 25% of the cabs that
// regularly run a site. Candidates now come from history; MDS is consulted only
// for "what is this specific cab doing today", which is the one thing it is
// authoritative about.

import { cabChain } from './_mds.mjs';
import { requireAuth } from './_auth.mjs';

const MAX_CABS = 20;          // ranked list head; checking more means the trip
                              // had no good candidates anyway
const CACHE_MS = 90_000;
const cache = new Map();      // `${cab}|${date}` -> { t, chain }

export default async (req) => {
  const denied = requireAuth(req);
  if (denied) return denied;
  if (req.method !== 'POST') return Response.json({ error: 'POST required' }, { status: 405 });

  let cabs = [], date = '';
  try { ({ cabs = [], date = '' } = await req.json()); } catch { /* rejected below */ }
  cabs = [...new Set(cabs.map((c) => String(c).trim()).filter(Boolean))];
  if (!cabs.length || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return Response.json({ error: 'cabs[] and date=YYYY-MM-DD required' }, { status: 400 });
  }
  if (cabs.length > MAX_CABS) {
    return Response.json({ error: `too many cabs (${cabs.length} > ${MAX_CABS})` }, { status: 400 });
  }

  // The day in IST, padded 6h either side so an overnight chain (a 23:45 logout
  // ending 01:10, or an early login staged from yesterday) is not cut mid-trip.
  const day0 = Date.parse(`${date}T00:00:00+05:30`) - 6 * 3600e3;
  const day1 = Date.parse(`${date}T00:00:00+05:30`) + 30 * 3600e3;

  const chains = {};
  const errors = {};
  await Promise.all(cabs.map(async (cab) => {
    const k = `${cab}|${date}`;
    const hit = cache.get(k);
    if (hit && Date.now() - hit.t < CACHE_MS) { chains[cab] = hit.chain; return; }
    try {
      const chain = await cabChain(cab, day0, day1);
      cache.set(k, { t: Date.now(), chain });
      chains[cab] = chain;
    } catch (e) {
      // Missing schedule must fail VISIBLY, not as an empty chain: an empty
      // chain reads as "completely free all day", which is exactly the wrong
      // default for a cab we could not check.
      errors[cab] = String(e.message);
    }
  }));

  return Response.json({
    date, chains,
    errors: Object.keys(errors).length ? errors : undefined,
    fetched_at: new Date().toISOString(),
  });
};

export const config = { path: '/api/sched' };
