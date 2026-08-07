# web/ — the deployable recommender screen

Static page + three small functions. No server, no database, no container.

```
browser                          Netlify function              upstream
───────────────────────────────  ────────────────────────────  ─────────────
index.html                       login.mjs   shared password
  └ scorer.js  ◄── all scoring   data.mjs    30d history       (built nightly
                                 wave.mjs    today's trips  ─► from Metabase)
                                 pool.mjs    candidate cabs ─► MDS
```

**The scoring runs in the browser.** That is the whole reason this needs no
server: the history a recommendation depends on is 196 KB gzipped for the
largest BU, so it is cheaper to send it to the deployer than to keep a process
alive to hold it. The functions do nothing but relay MDS and gate access.

## The pieces

| file | what it does |
|---|---|
| `public/index.html` | the screen — wave list, pickers, recommendation cards |
| `public/scorer.js` | **hand port of `../recommend.py`** — the ranking, feasibility, and wave-state logic |
| `scripts/build-history.mjs` | build step: Metabase card 130776 → one gzipped shard per BU |
| `scripts/verify.mjs` | replays a live fixture through `scorer.js` and requires it to match `recommend.py` exactly |
| `scripts/dev-server.mjs` | runs the real function handlers locally |
| `netlify/functions/_mds.mjs` | MDS auth + endpoints |
| `netlify/functions/_auth.mjs` | the shared-password check |

## Why `data/` is not under `public/`

The shards contain `anchor_pickup_geo` — the precise coordinate where the first
employee on each route is collected — for ~200k trips, keyed by cab and shift
time. Anything under `public/` on Netlify is a world-readable URL. So the shards
are bundled into the functions (`netlify.toml` → `included_files`) and served by
`data.mjs` behind the password instead.

That password is one shared secret for the team, not identity. It keeps the data
off the open internet; it does not tell you who looked at what.

## Local development

```bash
cd web
set -a; . ../.env; set +a
node scripts/build-history.mjs     # ~4 min, writes data/
node scripts/dev-server.mjs        # http://127.0.0.1:8770
```

`.env` needs `SITE_PASSWORD` in addition to what the Python side uses. The
Python service called the same thing `UI_PASSWORD`.

## Before changing anything in `scorer.js`

It is a port. `../recommend.py` is what was backtested — 41,203 trips, 26.6%
top-1 / 45.5% top-3 / 55.2% top-5 against real deployer choices. That number
only describes this file while the two produce identical rankings.

```bash
python3 ../verify_fixture.py ivycomptech-IVYHyd 10 > fixture.json
node scripts/verify.mjs fixture.json      # must print "0 failed"
```

The check captures real trips and real candidate pools and compares every scored
field, the ordering, and the rejection reasons. It has already caught two things
worth catching: a trip with no anchor coordinate (which crashed the Python and
would have produced silent `NaN` rankings in the JS), and a coordinate-precision
mismatch between the shard and the reference data.

Change a constant in one file, change it in the other, and re-run this.
