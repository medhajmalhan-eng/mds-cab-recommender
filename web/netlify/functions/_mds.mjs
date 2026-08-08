// Shared MDS client for the recommender's functions.
//
// Everything here was verified against production. The two non-obvious things,
// both of which cost real time to find:
//
//   1. `Authorization: <raw token>` — NO "Bearer" prefix. Chrome's HAR export
//      STRIPS Authorization headers, so this header is invisible in .har
//      captures and only appears in "Copy as cURL". Without it every endpoint
//      returns an empty-body 401 from Spring Security, which reads like a bad
//      token rather than a missing header.
//
//   2. /fis/auth/login returns HTTP 200 even on FAILURE, with the real result
//      in `successStatus`. Data endpoints DO use a proper 401. So auth failure
//      has to be detected two different ways depending on the endpoint.
//
//   3. trip/filter rejects more than 10 shifts per call with a 500 — batch.
//
// Env: MDS_EMAIL (or MDS_USERNAME) + MDS_PASSWORD.

export const BASE = process.env.MDS_BASE_URL || 'https://fleet-green.moveinsync.com';

// Module scope survives warm invocations, so a hot function reuses the token
// rather than logging in on every request. Cold starts re-login; that is fine
// (~400 ms) and is why the token is not persisted anywhere.
let cachedToken = null;
let loginInFlight = null;

export function credentials() {
  const username = process.env.MDS_EMAIL || process.env.MDS_USERNAME;
  const password = process.env.MDS_PASSWORD;
  if (!username || !password) {
    const seen = Object.keys(process.env).filter((k) => k.startsWith('MDS_'));
    throw new Error(
      `MDS credentials not configured. Need MDS_EMAIL (or MDS_USERNAME) + MDS_PASSWORD. ` +
      `This function currently sees: ${seen.length ? seen.join(', ') : 'no MDS_* variables at all'}. ` +
      `If you set them in Netlify, check the variable Scope includes "Functions", then redeploy — ` +
      `functions capture environment variables at deploy time, not per request.`);
  }
  return { username, password };
}

async function login() {
  const { username, password } = credentials();
  const res = await fetch(`${BASE}/fis/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const json = await res.json();                       // 200 even when rejected
  if (!json.successStatus || !json.data?.token) {
    throw new Error('MDS login rejected: ' + (json.message || 'no token returned') +
                    ' — the MDS password has probably been rotated.');
  }
  const d = json.data;
  return {
    Authorization: d.token,                            // raw, no "Bearer"
    'x-cds-token': d.token,
    user_detail: d.userDetailToken,
    vendor_id: String(d.vendorId),
    Accept: 'application/json, text/plain, */*',
    Referer: `${BASE}/cds-green.html`,
  };
}

// Collapse concurrent logins: wave.mjs fires ~28 parallel calls, and without
// this a cold start would attempt 28 simultaneous logins.
function refreshToken() {
  if (!loginInFlight) {
    loginInFlight = login()
      .then((t) => { cachedToken = t; return t; })
      .finally(() => { loginInFlight = null; });
  }
  return loginInFlight;
}

export async function call(path, body, retried = false) {
  if (!cachedToken) await refreshToken();
  const res = await fetch(BASE + path, {
    method: body ? 'POST' : 'GET',
    headers: body ? { ...cachedToken, 'Content-Type': 'application/json' } : cachedToken,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !retried) {                 // token expired (4h life)
    cachedToken = null;
    await refreshToken();
    return call(path, body, true);
  }
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

// Vendor mappings change on the order of months. Caching them removes one
// serial round-trip from every cold call, which matters when the whole handler
// has to finish inside the function timeout.
const guidCache = new Map();
const GUID_TTL = 6 * 3600_000;

export async function vendorGuids(buid) {
  const hit = guidCache.get(buid);
  if (hit && Date.now() - hit.t < GUID_TTL) return hit.guids;
  const r = await call('/fis/vendor/vendorowner/mapping/assigned');
  const m = (r.data?.vendorOwnerMappings || []).find((x) => x.buid === buid);
  if (!m) throw new Error(`no vendor mapping for ${buid} — is this BU assigned to your MDS login?`);
  const guids = m.assignedVendors.map((v) => v.vendorGuid);
  guidCache.set(buid, { t: Date.now(), guids });
  return guids;
}

export async function shifts(buid, guids, dayMs) {
  const r = await call('/fis/cds/shift/v3?nonShift=false&isPlannedTrip=false',
    { dates: [dayMs], buidsAndVendors: [{ businessUnit: buid, vendorGuids: guids }] });
  const out = [];
  for (const d of r || []) {
    for (const b of d.businessUnitOfficeShifts || []) {
      for (const o of b.officeShifts || []) out.push(o);
    }
  }
  return out;
}

// The 'shiftsByOfficeDirectionByBuid' shape is NOT a selectedShifts list — it is
// {buid: [{office:{name,id}, shiftsByDirection:{loginShifts,logoutShifts}}]} and
// `directions` must match whichever list is populated. Getting this wrong is a
// 400; sending more than 10 shifts is a 500.
export async function trips(buid, guids, dayMs) {
  const jobs = [];
  for (const off of await shifts(buid, guids, dayMs)) {
    for (const [direction, key] of [['IN', 'loginShifts'], ['OUT', 'logoutShifts']]) {
      const times = (off[key] || []).map((s) => s.shiftTime);
      for (let i = 0; i < times.length; i += 10) {
        const chunk = times.slice(i, i + 10);
        jobs.push({
          dateList: [dayMs, dayMs],
          businessUnitVendorsList: [{ businessUnit: buid, vendorGuids: guids }],
          directions: [direction],
          shiftsByOfficeDirectionByBuid: {
            [buid]: [{
              office: { name: off.officeName, id: off.officeId },
              shiftsByDirection: {
                loginShifts: direction === 'IN' ? chunk : [],
                logoutShifts: direction === 'IN' ? [] : chunk,
              },
            }],
          },
          selectedShifts: chunk.map((t) => ({
            buid, office: off.officeName, shift: t,
            officeId: off.officeId, direction,
          })),
          plannedTrip: false,
        });
      }
    }
  }
  // Parallel: a large BU is ~28 of these and serial would blow the timeout.
  const results = await Promise.all(jobs.map((b) =>
    call('/fis/cds/trip/filter?source=web&nonShift=false&cabUnassigned=false', b)
      .catch(() => ({ data: [] }))));
  const out = [], seen = new Set();
  for (const r of results) {
    for (const t of r.data || []) {
      if (!seen.has(t.tripId)) { seen.add(t.tripId); out.push(t); }
    }
  }
  return out;
}

// distance=true is REQUIRED: with distance=false, emptyLegInMetres comes back
// as -1 for every cab and the deadhead gate silently stops filtering.
//
// NOTE: no longer the candidate source. Measured on 228 live predictions, this
// list omitted a median 25% of the cabs that regularly run the site (one site:
// 53 regulars, zero offered). Candidates now come from history
// (scorer.candidatesFor); this remains only as a cross-check.
export async function vehicles(buid, tripGuid) {
  const g = encodeURIComponent(tripGuid).replace(/%24/g, '$');
  const r = await call(`/fis/cds/vendor/trips/${buid}/${g}/vehicles?distance=true`);
  return r?.data || [];
}

/**
 * One cab's schedule for a time window — Completed, Ongoing AND future Planned
 * trips, across ALL BUs, each with start/end times and pickup/drop geocodes.
 * Verified live 2026-08-08 (TG-08-V-2063: completed 10:00, ongoing 11:30,
 * planned 14:20 — all in one response).
 *
 * This is the real-time feasibility primitive: everything the old pool's
 * busyVehicle/vehicleNextTripDetails pretended to be, except with locations,
 * dates, and cross-BU visibility.
 *
 * Returns a chain in the exact shape feasible() consumes, sorted by start.
 */
export async function cabChain(registration, startMs, endMs) {
  const r = await call('/fis/cds/trip/cab/trip-history',
    { registration, startTime: startMs, endTime: endMs, allBuids: true });
  const geo = (s) => {
    const [a, b] = String(s || '').split(',').map(Number);
    return Number.isFinite(a) && Number.isFinite(b) ? [a, b] : [null, null];
  };
  const chain = [];
  for (const trips of Object.values(r || {})) {
    for (const t of trips || []) {
      if (!t || !t.startTime || !t.endTime) continue;
      if (t.tripStatus === 'Cancelled') continue;
      const emp = t.employees || [];
      const [slat, slng] = geo(emp[0]?.pickupLoc?.geoCord);
      const [elat, elng] = geo(emp[emp.length - 1]?.dropLoc?.geoCord);
      chain.push({
        start: t.startTime / 1000, end: t.endTime / 1000,
        slat, slng, elat, elng,
        label: `${t.direction || ''} ${t.shift || ''} (${t.buid || '?'})`.trim(),
        tripId: t.tripId, status: t.tripStatus,
      });
    }
  }
  chain.sort((a, b) => a.start - b.start);
  return chain;
}
