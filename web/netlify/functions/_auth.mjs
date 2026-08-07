// Shared-password gate.
//
// WHY THIS EXISTS: the history shards carry anchor_pickup_geo — the precise
// coordinate where the first employee on each route is collected — for ~200k
// trips, keyed by cab registration and shift time. Anything under public/ on
// Netlify is a world-readable URL, so publishing the shards as static files
// would put that dataset on the open internet. Serving them through a function
// behind this check keeps the same protection the Python service had.
//
// WHAT THIS IS NOT: identity. It is one shared password for the whole team, the
// same model as the UI_PASSWORD in the Python service. It stops the data being
// publicly indexable; it does not tell you who looked at what. If this screen
// outlives the pilot, the right fix is a real SSO in front of it.
//
// The cookie is an HMAC over a fixed string keyed by the password itself, so
// there is no session store to keep and rotating SITE_PASSWORD invalidates
// every outstanding cookie for free.

import { createHmac, timingSafeEqual } from 'node:crypto';

const COOKIE = 'cr_auth';
const MAX_AGE = 12 * 3600;          // one shift; deployers re-enter next day

const secret = () => process.env.SITE_PASSWORD || '';

const sign = () => createHmac('sha256', secret()).update('cab-recommender-v1').digest('hex');

function safeEqual(a, b) {
  const x = Buffer.from(String(a));
  const y = Buffer.from(String(b));
  // timingSafeEqual throws on length mismatch, which would itself leak length
  return x.length === y.length && timingSafeEqual(x, y);
}

/** null when authorised, otherwise a Response to return immediately. */
export function requireAuth(req) {
  const pw = secret();
  if (!pw) {
    // Fail CLOSED. An unset password must not silently mean "no protection" —
    // that is precisely how the geocodes would end up public by accident.
    return Response.json({
      error: 'SITE_PASSWORD is not configured on this site. Set it in Netlify -> ' +
             'Site configuration -> Environment variables (scope: Functions), then redeploy.',
    }, { status: 503 });
  }
  const cookies = Object.fromEntries(
    (req.headers.get('cookie') || '').split(';').map((c) => {
      const i = c.indexOf('=');
      return i < 0 ? [c.trim(), ''] : [c.slice(0, i).trim(), c.slice(i + 1).trim()];
    }));
  if (safeEqual(cookies[COOKIE] || '', sign())) return null;
  return Response.json({ error: 'auth required' }, { status: 401 });
}

export function issueCookie(password) {
  const pw = secret();
  if (!pw || !safeEqual(password || '', pw)) return null;
  return `${COOKIE}=${sign()}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${MAX_AGE}`;
}
