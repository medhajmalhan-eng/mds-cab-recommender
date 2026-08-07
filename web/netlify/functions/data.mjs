// GET /api/data?buid=<buid>   -> that BU's 30-day history shard (gzipped JSON)
// GET /api/data               -> the BU index (buids.json)
//
// These files are built by scripts/build-history.mjs into web/data/ and bundled
// into this function via netlify.toml -> included_files. They deliberately do
// NOT live under public/: they carry anchor_pickup_geo for ~200k trips, and
// public/ on Netlify is a world-readable URL. See _auth.mjs.
//
// The payload is stored already-gzipped and passed straight through with
// Content-Encoding: gzip — no decompress/recompress in the function, and the
// browser handles it natively. ~230 KB for the largest BU.

import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { requireAuth } from './_auth.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// Netlify preserves the repo layout inside the bundle, so data/ sits two levels
// up from netlify/functions/. Fall back to cwd for `netlify dev` and the local
// dev server, which run from the project root.
const CANDIDATES = [
  path.resolve(HERE, '..', '..', 'data'),
  path.resolve(process.cwd(), 'data'),
];

async function load(name) {
  let lastErr;
  for (const dir of CANDIDATES) {
    try { return await readFile(path.join(dir, name)); } catch (e) { lastErr = e; }
  }
  throw lastErr;
}

export default async (req) => {
  const denied = requireAuth(req);
  if (denied) return denied;

  const buid = new URL(req.url).searchParams.get('buid');
  try {
    if (!buid) {
      const body = await load('buids.json');
      return new Response(body, {
        headers: { 'Content-Type': 'application/json', 'Cache-Control': 'private, max-age=300' },
      });
    }
    // Path traversal guard: buid lands in a filename, and "../../etc/passwd"
    // would otherwise be a readable path. BUIDs are [a-z0-9-] in practice.
    if (!/^[A-Za-z0-9._-]+$/.test(buid)) {
      return Response.json({ error: 'bad buid' }, { status: 400 });
    }
    const body = await load(`${buid}.json.gz`);
    return new Response(body, {
      headers: {
        'Content-Type': 'application/json',
        'Content-Encoding': 'gzip',
        // private: it is behind a password, so no shared CDN caching. The
        // browser still reuses it for the session, which is what matters —
        // the shard only changes once a day at build time.
        'Cache-Control': 'private, max-age=1800',
      },
    });
  } catch (e) {
    return Response.json({
      error: `no history for ${buid || 'index'} — has the build run? (${e.code || e.message})`,
    }, { status: 404 });
  }
};

export const config = { path: '/api/data' };
