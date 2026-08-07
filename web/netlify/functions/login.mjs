// POST /api/login   {"password": "..."}   -> sets the auth cookie
// GET  /api/login                         -> {authed: bool}, for the UI to skip
//                                            the prompt on a warm reload
import { requireAuth, issueCookie } from './_auth.mjs';

export default async (req) => {
  if (req.method === 'GET') {
    const denied = requireAuth(req);
    // 503 means SITE_PASSWORD is missing — surface that rather than reporting a
    // plain "not logged in", which would send the deployer round a loop typing a
    // password that cannot possibly work.
    if (denied && denied.status === 503) return denied;
    return Response.json({ authed: !denied });
  }
  if (req.method !== 'POST') return Response.json({ error: 'POST required' }, { status: 405 });

  let password = '';
  try { ({ password } = await req.json()); } catch { /* empty body -> reject below */ }

  const cookie = issueCookie(password);
  if (!cookie) {
    // Deliberately slow: a shared password with no rate limiting is guessable at
    // machine speed otherwise. ~500ms caps an attacker at a couple of attempts a
    // second per connection while being invisible to someone typing it once.
    await new Promise((r) => setTimeout(r, 500));
    return Response.json({ error: 'wrong password' }, { status: 401 });
  }
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { 'Content-Type': 'application/json', 'Set-Cookie': cookie },
  });
};

export const config = { path: '/api/login' };
