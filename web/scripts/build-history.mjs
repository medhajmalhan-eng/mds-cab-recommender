// Build-time history pull: Metabase card 130776 -> one JSON shard per BU.
//
// This replaces sync.py + history.db for the deployed screen. The scorer only
// ever reads five fields per trip, so 30 days of a large BU is ~230 KB gzipped
// — small enough to be a static file the CDN serves, which is why this project
// needs no server at all.
//
// Why build-time rather than a function: the extract is ~7k rows/day x 30 days
// and takes minutes to come back from Metabase. A sync function has ~10s.
//
// Auth: the Apps Script proxy holds the Metabase service-account session
// (MISLib) and exposes it as plain HTTP. Same proxy the VCF dashboard uses.
//
// Env (Netlify -> Site configuration -> Environment variables):
//   APPS_SCRIPT_EXEC_URL   the /exec URL of the proxy
//   APPS_SCRIPT_TOKEN      its shared token
//   METABASE_CARD_ID       default 130776
//   HISTORY_WINDOW_DAYS    default 30
//
// Run: node scripts/build-history.mjs
import { mkdir, writeFile, rm } from 'node:fs/promises';
import { gzipSync } from 'node:zlib';
import path from 'node:path';

// NOT under public/. These shards carry anchor_pickup_geo — the precise
// coordinate where the first employee on each route is collected — and anything
// under public/ on Netlify is a world-readable URL. They are bundled into the
// functions instead (netlify.toml -> included_files) and served by data.mjs
// behind the shared-password check. Stored gzipped: 32 BUs is ~30 MB raw but
// ~6 MB compressed, comfortably inside the function bundle limit, and the
// browser decompresses it natively from Content-Encoding.
const OUT = path.join(process.cwd(), 'data');
const EXEC = process.env.APPS_SCRIPT_EXEC_URL || '';
const TOKEN = process.env.APPS_SCRIPT_TOKEN || '';
const CARD = process.env.METABASE_CARD_ID || '130776';
const WINDOW_DAYS = +(process.env.HISTORY_WINDOW_DAYS || 30);
const CONCURRENCY = 6;            // the proxy is a Google Apps Script; be polite
const MAX_MISSING_DAYS = 3;       // a hole or two is survivable, a gap is not

const iso = (d) => d.toISOString().slice(0, 10);

// Coordinate precision in the shard. 6 dp is ~11 cm at this latitude — finer
// than the GPS fix that produced the data, so it is lossless with respect to
// the source while keeping the JSON compact. It was 5 dp (~1.1 m), which was
// enough to move kernel scores by ~1e-4 relative and made the JS and Python
// scorers disagree on inputs that were supposed to be identical.
// verify_fixture.py rounds to GEO_DP too, so the two are compared on exactly
// the values that ship.
export const GEO_DP = 6;
const r6 = (x) => +x.toFixed(GEO_DP);

// ── CSV parsing ──────────────────────────────────────────────────────────
// Hand-rolled because the extract contains quoted fields with embedded commas
// (office names, delay reasons) and split(',') silently shifts every later
// column on those rows.
function parseCSV(text) {
  const rows = [];
  let row = [], field = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; } else quoted = false;
      } else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') { row.push(field); field = ''; }
    else if (ch === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
    else if (ch !== '\r') field += ch;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return [];
  const head = rows.shift().map((h) => h.trim());
  return rows
    .filter((r) => r.length === head.length)
    .map((r) => Object.fromEntries(head.map((h, i) => [h, r[i]])));
}

// Mirrors sync.py clean(): the extract carries the STRING 'null' rather than an
// empty cell for missing values, so a plain truthiness check lets "null" through
// and it ends up compared against real shift times.
const clean = (s) => {
  const v = String(s ?? '').trim();
  return ['', 'null', 'none', 'na', 'n/a'].includes(v.toLowerCase()) ? null : v;
};

const geo = (s) => {
  const v = clean(s);
  if (!v) return [null, null];
  const [a, b] = v.split(',').map(Number);
  return Number.isFinite(a) && Number.isFinite(b) ? [a, b] : [null, null];
};

async function fetchDay(day, attempt = 1) {
  const p = new URLSearchParams({
    route: 'card', id: CARD, token: TOKEN,
    start: day, end: iso(new Date(Date.parse(day) + 86400e3)),
  });
  try {
    const res = await fetch(`${EXEC}?${p}`, {
      redirect: 'follow', signal: AbortSignal.timeout(420000),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    // The proxy returns HTTP 200 with a JSON error envelope on failure, so the
    // status code alone cannot distinguish success from failure.
    if (text.trimStart().startsWith('{')) throw new Error(text.slice(0, 200));
    return parseCSV(text);
  } catch (e) {
    if (attempt < 3) {
      console.warn(`   retry ${attempt} for ${day}: ${String(e.message).slice(0, 120)}`);
      await new Promise((r) => setTimeout(r, 5000 * attempt));
      return fetchDay(day, attempt + 1);
    }
    throw e;
  }
}

async function pool(items, n, fn) {
  const out = new Array(items.length);
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) {
      const k = i++;
      out[k] = await fn(items[k], k);
    }
  }));
  return out;
}

async function main() {
  if (!EXEC || !TOKEN) {
    // Hard failure, not a skip. public/data is gitignored build output, so on a
    // clean CI checkout there is nothing to fall back to — skipping would deploy
    // a screen whose every shard 404s and which silently recommends nothing.
    console.error('APPS_SCRIPT_EXEC_URL / APPS_SCRIPT_TOKEN are not set.');
    console.error('Set them in Netlify -> Site configuration -> Environment variables.');
    process.exit(1);
  }

  const today = new Date();
  const days = Array.from({ length: WINDOW_DAYS }, (_, k) =>
    iso(new Date(today.getTime() - (k + 1) * 86400e3))).reverse();

  console.log(`history: ${days[0]} .. ${days[days.length - 1]} (${WINDOW_DAYS}d), card ${CARD}`);

  const failed = [];
  const perDay = await pool(days, CONCURRENCY, async (d) => {
    try {
      const rows = await fetchDay(d);
      console.log(`   ${d}  ${rows.length} rows`);
      return { day: d, rows };
    } catch (e) {
      console.error(`   ${d}  FAILED: ${String(e.message).slice(0, 160)}`);
      failed.push(d);
      return { day: d, rows: [] };
    }
  });

  if (failed.length > MAX_MISSING_DAYS) {
    console.error(`\n${failed.length} of ${WINDOW_DAYS} days failed (${failed.join(', ')}).`);
    console.error('Refusing to publish a history with that big a hole — recency weighting');
    console.error('would quietly favour whichever cabs happen to sit in the days that landed.');
    process.exit(1);
  }

  // ── reshape into per-BU shards ──
  // Row layout is positional to keep the payload small:
  //   [cabIdx, lat, lng, shift, ageDays]
  // Cab registrations repeat thousands of times, so they live in a shared
  // string table per shard and rows carry an index. Halves the raw size.
  const bus = new Map();   // buid -> { shards: Map<"office|DIR", rows>, cabs, faults }
  let kept = 0, skippedNoAnchor = 0, skippedNoCab = 0, dupes = 0;

  for (const { day, rows } of perDay) {
    const age = Math.round((Date.parse(`${iso(today)}T00:00:00Z`) - Date.parse(`${day}T00:00:00Z`)) / 86400e3);
    // (trip_id, bunit_id) is the unique key — trip_id alone is NOT unique across
    // business units. The extract occasionally returns a trip twice within a day
    // (the subvendor join can fan out), and a duplicate row would count twice in
    // the kernel, quietly inflating that cab's score. sync.py gets this for free
    // from the table's primary key; here it has to be explicit.
    const seenDay = new Set();
    for (const r of rows) {
      const uk = `${r.trip_id}|${r.bunit_id}`;
      if (seenDay.has(uk)) { dupes++; continue; }
      seenDay.add(uk);
      const dir = (r.trip_direction || '').toUpperCase();
      const [lat, lng] = geo(dir === 'LOGIN' ? r.anchor_pickup_geo : r.anchor_drop_geo);
      if (lat === null) { skippedNoAnchor++; continue; }
      // TRIM matters: 57 rows in the reference DB carry a leading space, and an
      // untrimmed key never matches MDS's cabRegNo — that cab's whole history
      // would be invisible to the scorer.
      const cab = clean(r.actual_cab_registration)?.trim();
      if (!cab) { skippedNoCab++; continue; }
      const buid = clean(r.bunit_id);
      const office = clean(r.office);
      if (!buid || !office) continue;

      let b = bus.get(buid);
      if (!b) { b = { shards: new Map(), cabIdx: new Map(), cabs: [], stat: new Map() }; bus.set(buid, b); }

      let ci = b.cabIdx.get(cab);
      if (ci === undefined) { ci = b.cabs.push(cab) - 1; b.cabIdx.set(cab, ci); }

      const key = `${office}|${dir}`;
      let s = b.shards.get(key);
      if (!s) { s = []; b.shards.set(key, s); }
      s.push([ci, r6(lat), r6(lng), clean(r.shift), age]);

      // cab_profiles equivalent: per (cab, office) trip count + driver-fault rate.
      // Only the fault rate is read by the scorer (as a tiebreak), but n_trips is
      // what makes a 1-in-1 fault rate distinguishable from 3-in-30.
      const pk = `${ci}|${office}`;
      let st = b.stat.get(pk);
      if (!st) { st = [0, 0]; b.stat.set(pk, st); }
      st[0]++;
      if (r.delay_reason === 'DRIVER') st[1]++;
      kept++;
    }
  }

  await rm(OUT, { recursive: true, force: true });
  await mkdir(OUT, { recursive: true });

  const index = [];
  for (const [buid, b] of bus) {
    const faults = {};
    for (const [pk, [n, f]] of b.stat) {
      const [ci, office] = pk.split('|');
      (faults[office] ||= {})[ci] = [n, +(f / n).toFixed(4)];
    }
    const payload = {
      buid,
      built_at: new Date().toISOString(),
      window_days: WINDOW_DAYS,
      from: days[0], to: days[days.length - 1],
      cabs: b.cabs,
      shards: Object.fromEntries(b.shards),
      faults,
    };
    const json = JSON.stringify(payload);
    const gz = gzipSync(json, { level: 9 });
    await writeFile(path.join(OUT, `${buid}.json.gz`), gz);
    const n = [...b.shards.values()].reduce((a, s) => a + s.length, 0);
    index.push({
      buid, trips_30d: n,
      offices: new Set([...b.shards.keys()].map((k) => k.split('|')[0])).size,
      bytes: json.length, gz: gz.length,
    });
  }

  index.sort((a, b) => b.trips_30d - a.trips_30d);
  await writeFile(path.join(OUT, 'buids.json'), JSON.stringify({
    built_at: new Date().toISOString(),
    window_days: WINDOW_DAYS,
    from: days[0], to: days[days.length - 1],
    missing_days: failed,
    buids: index,
  }, null, 1));

  const totalGz = index.reduce((a, r) => a + r.gz, 0);
  console.log(`\n${kept} trips -> ${bus.size} BU shards in data/ (gzipped, served by data.mjs)`);
  console.log(`   skipped: ${skippedNoAnchor} no anchor geo, ${skippedNoCab} no cab, ${dupes} duplicate rows`);
  console.log(`   bundle: ${(totalGz / 1e6).toFixed(1)} MB compressed`);
  if (failed.length) console.log(`   WARNING: ${failed.length} day(s) missing: ${failed.join(', ')}`);
  for (const r of index.slice(0, 5)) {
    console.log(`   ${r.buid.padEnd(24)} ${String(r.trips_30d).padStart(6)} trips  ` +
                `${(r.bytes / 1e6).toFixed(2)} MB -> ${(r.gz / 1e3).toFixed(0)} KB`);
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
