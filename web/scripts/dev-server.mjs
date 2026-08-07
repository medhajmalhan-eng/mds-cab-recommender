// Local stand-in for Netlify: serves public/ and routes /api/* to the same
// function modules the deployed site uses.
//
//   set -a; . ../.env; set +a
//   node scripts/dev-server.mjs           -> http://127.0.0.1:8770
//
// Deliberately imports the real handlers rather than reimplementing them, so
// what is tested here is what ships. It does not emulate Netlify's bundling,
// timeouts, or edge cache — those are the things worth checking on a deploy
// preview rather than locally.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

const PORT = +(process.env.PORT || 8770);
const PUBLIC = path.join(process.cwd(), 'public');

const ROUTES = {
  '/api/login': () => import('../netlify/functions/login.mjs'),
  '/api/data': () => import('../netlify/functions/data.mjs'),
  '/api/wave': () => import('../netlify/functions/wave.mjs'),
  '/api/pool': () => import('../netlify/functions/pool.mjs'),
};

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json',
  '.css': 'text/css; charset=utf-8',
};

createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    const route = ROUTES[url.pathname];
    if (route) {
      const body = ['POST', 'PUT', 'PATCH'].includes(req.method)
        ? await new Promise((resolve) => {
            let b = ''; req.on('data', (c) => { b += c; }); req.on('end', () => resolve(b));
          })
        : undefined;
      const handler = (await route()).default;
      const out = await handler(new Request(url, {
        method: req.method,
        headers: Object.entries(req.headers).filter(([, v]) => typeof v === 'string'),
        body,
      }));
      // Netlify collapses multiple Set-Cookie headers itself; here one is enough.
      const headers = Object.fromEntries(out.headers);
      res.writeHead(out.status, headers);
      res.end(Buffer.from(await out.arrayBuffer()));
      return;
    }

    let p = url.pathname === '/' ? '/index.html' : url.pathname;
    // Traversal guard — this serves from the filesystem on a dev machine.
    const file = path.join(PUBLIC, path.normalize(p).replace(/^(\.\.[/\\])+/, ''));
    if (!file.startsWith(PUBLIC)) { res.writeHead(403).end('forbidden'); return; }
    const buf = await readFile(file);
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(buf);
  } catch (e) {
    if (e.code === 'ENOENT') { res.writeHead(404).end('not found'); return; }
    console.error(e);
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: String(e.message) }));
  }
}).listen(PORT, '127.0.0.1', () => {
  const missing = ['SITE_PASSWORD', 'MDS_PASSWORD'].filter((k) => !process.env[k]);
  console.log(`dev server on http://127.0.0.1:${PORT}`);
  if (missing.length) {
    console.log(`!! ${missing.join(', ')} not set — did you 'set -a; . ../.env; set +a'?`);
  }
});
