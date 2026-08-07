// GET /api/pool?buid=<buid>&guid=<tripGuid>[,<tripGuid>...]
//
// Live candidate cabs from MDS, one pool per trip GUID.
//
// A pool CANNOT be shared between trips: emptyLegInMetres is computed relative
// to whichever trip was queried, so reusing another trip's pool would report
// every deadhead against the wrong origin and the distance gate would filter
// the wrong cabs.
//
// Layer 2 (cross-vendor) is why this takes a list. There is no vendor parameter
// on the MDS endpoint, so another vendor's cabs are only visible through one of
// that vendor's own trips. The caller picks one proxy trip per vendor and asks
// for them together; fetching them in one invocation keeps it to a single round
// trip from the browser.

import { vehicles } from './_mds.mjs';
import { requireAuth } from './_auth.mjs';

const CACHE_MS = 120_000;     // pools change more slowly than assignments
const MAX_GUIDS = 12;         // ~12 vendors is already an unusually broad BU
const cache = new Map();

function cached(buid, guid) {
  const hit = cache.get(`${buid}|${guid}`);
  return hit && Date.now() - hit.t < CACHE_MS ? hit.data : null;
}

export default async (req) => {
  const denied = requireAuth(req);
  if (denied) return denied;

  const url = new URL(req.url);
  const buid = url.searchParams.get('buid');
  const guids = (url.searchParams.get('guid') || '').split(',').map((s) => s.trim()).filter(Boolean);
  if (!buid || !guids.length) {
    return Response.json({ error: 'buid and guid required' }, { status: 400 });
  }
  if (guids.length > MAX_GUIDS) {
    return Response.json({ error: `too many guids (${guids.length} > ${MAX_GUIDS})` }, { status: 400 });
  }

  try {
    const pools = {};
    const errors = {};
    await Promise.all(guids.map(async (g) => {
      const hit = cached(buid, g);
      if (hit) { pools[g] = hit; return; }
      try {
        const cabs = await vehicles(buid, g);
        // Trim to what the scorer reads. A raw pool is ~250 cabs of large
        // objects; this cuts the payload by roughly 8x and the browser never
        // touches the dropped fields.
        const slim = cabs.map((c) => ({
          cabRegNo: c.cabRegNo,
          capacity: c.capacity,
          cabActive: c.cabActive,
          virtual: c.virtual,
          busyVehicle: c.busyVehicle,
          complianceStatus: c.complianceStatus,
          emptyLegInMetres: c.emptyLegInMetres,
          subVendorName: c.subVendorName,
          driver: c.drivers?.[0]?.driverName || null,
          // {hour, min, direction, buid} with NO location and no date — the
          // weaker "time-only" feasibility signal. See feasible() in scorer.js.
          nextTrip: c.vehicleNextTripDetails
            ? {
                hour: c.vehicleNextTripDetails.hour,
                min: c.vehicleNextTripDetails.min,
                buid: c.vehicleNextTripDetails.buid,
              }
            : null,
        }));
        cache.set(`${buid}|${g}`, { t: Date.now(), data: slim });
        pools[g] = slim;
      } catch (e) {
        // One vendor's pool failing must not lose the others — Layer 2 on four
        // vendors is still useful when the fifth errors, as long as we say so.
        errors[g] = String(e.message);
        pools[g] = [];
      }
    }));

    return Response.json({
      buid, pools,
      errors: Object.keys(errors).length ? errors : undefined,
      fetched_at: new Date().toISOString(),
    });
  } catch (e) {
    return Response.json({ buid, pools: {}, error: String(e.message) });
  }
};

export const config = { path: '/api/pool' };
