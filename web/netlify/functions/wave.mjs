// GET /api/wave?buid=<buid>&date=YYYY-MM-DD
//
// Today's trips for one BU, trimmed to the fields the scorer and UI actually
// read. The browser derives wave state from this — which cabs are already
// committed in each (shift, direction), and each cab's chain of trips with
// geocodes for the spatial feasibility check.
//
// Deriving in the browser rather than here is deliberate: that derivation has
// to match recommend.py exactly, so it belongs next to the ported scorer where
// it can be diffed and tested, not split across a network boundary.
//
// WAVE STATE, and why it is rebuilt from scratch every poll:
// The deployer works in the MDS tab, not ours, so assignments have to be
// observed rather than received. Rebuilding from trip/filter each time means
// un-assigning a cab releases it again. A monotonic "already used" set would
// keep a released cab hidden for the rest of the session.

import { trips, vendorGuids } from './_mds.mjs';
import { requireAuth } from './_auth.mjs';

const CACHE_MS = 25_000;      // just under the UI's 30s poll, so a refresh is fresh
const cache = new Map();      // `${buid}|${date}` -> { t, payload }

export default async (req) => {
  const denied = requireAuth(req);
  if (denied) return denied;

  const url = new URL(req.url);
  const buid = url.searchParams.get('buid');
  const date = url.searchParams.get('date') || new Date().toISOString().slice(0, 10);
  if (!buid) return Response.json({ error: 'buid required' }, { status: 400 });

  const key = `${buid}|${date}`;
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < CACHE_MS && url.searchParams.get('force') !== '1') {
    return Response.json({ ...hit.payload, cached: true });
  }

  try {
    // Local midnight for the BU's day. MDS keys everything on epoch ms and the
    // function runs in UTC, so this is pinned to IST rather than the runtime's
    // idea of local time — otherwise the "day" silently shifts by 5.5h and the
    // late-evening waves a midnight deployer works land on the wrong date.
    const dayMs = Date.parse(`${date}T00:00:00+05:30`);
    const guids = await vendorGuids(buid);
    const raw = await trips(buid, guids, dayMs);

    const payload = {
      buid, date,
      fetched_at: new Date().toISOString(),
      count: raw.length,
      trips: raw.map((t) => ({
        tripId: t.tripId,
        tripGuid: t.tripGuid,
        direction: t.tripDirection,
        shiftTime: t.shiftTime,
        office: t.officeName,
        vendor: t.vendorName,
        capacity: t.plannedCabCapacity,
        cutOff: t.assignmentCutOffTime,
        start: t.tripStartTime,
        end: t.tripEndTime,
        startGeo: t.tripStartGeoCord,
        endGeo: t.tripEndGeoCord,
        // For a logout the meaningful endpoint is the DROP, not the pickup —
        // showing pickupLandmark there rendered "NA" on every OUT trip.
        landmark: t.tripDirection === 'IN' ? t.pickupLandmark : t.dropLandmark,
        assigned: !!t.cabAssigned,
        cab: t.cabReg,
      })),
    };
    cache.set(key, { t: Date.now(), payload });
    return Response.json(payload);
  } catch (e) {
    // 200 with an `error` field, not a 5xx: the UI renders this message in the
    // banner. A 502 would surface as an opaque network failure in the console
    // and tell the deployer nothing about what to do.
    return Response.json({ buid, date, trips: [], error: String(e.message) });
  }
};

export const config = { path: '/api/wave' };
