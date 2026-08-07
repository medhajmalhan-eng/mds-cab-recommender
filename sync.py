#!/usr/bin/env python3
"""
Metabase -> SQLite sync for the MDS cab recommender.

Pulls trip history through the Apps Script proxy (which holds the Metabase
service-account auth) and maintains a rolling window in SQLite. The proxy is a
dumb pipe; all state and scheduling live here.

    python3 sync.py --backfill        seed the full window, one day per call
    python3 sync.py                   append yesterday, prune, rebuild profiles
    python3 sync.py --day 2026-08-05  re-pull one specific day (idempotent)

Schedule with cron in the EVENING, not after midnight — deployers work past
midnight on early-morning shifts and a 00:30 run would leave them a day staler:

    0 22 * * *  cd ~/mds-cab-recommender && /usr/bin/python3 sync.py >> data/sync.log 2>&1
"""
import argparse, csv, io, os, sqlite3, sys, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(ROOT, "data", "history.db")


def cfg():
    """Same .env-or-environment resolution as mds.load_env, so the sync runs
    unchanged in a container where secrets arrive as env vars."""
    d = {}
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        d = dict(l.strip().split("=", 1) for l in open(p)
                 if "=" in l and not l.strip().startswith("#"))
    for k in ("APPS_SCRIPT_EXEC_URL", "APPS_SCRIPT_TOKEN", "METABASE_CARD_ID",
              "HISTORY_WINDOW_DAYS"):
        if os.environ.get(k):
            d[k] = os.environ[k]
    for k in ("APPS_SCRIPT_EXEC_URL", "APPS_SCRIPT_TOKEN", "METABASE_CARD_ID"):
        if not d.get(k):
            sys.exit("%s not set (via .env or environment)" % k)
    return d


# ─────────────────────────────── schema ────────────────────────────────
# Geos are split into floats up front so scoring never re-parses strings.
DDL = """
CREATE TABLE IF NOT EXISTS trips (
  -- (trip_id, bunit_id) is the unique key. trip_id alone is NOT unique across
  -- business units, and using it as the PK silently drops colliding rows.
  trip_id            TEXT NOT NULL,
  bunit_id           TEXT NOT NULL,
  day                TEXT NOT NULL,
  office TEXT, shift TEXT, trip_direction TEXT,
  planned_start_time TEXT, planned_end_time TEXT, cab_allocation_time TEXT,
  cab_reg            TEXT, driver_id TEXT,
  vendor_id          TEXT, subvendor_name TEXT, eff_vendor TEXT,
  capacity           INTEGER, desired_capacity INTEGER, cabtype TEXT,
  anchor_lat REAL, anchor_lng REAL, office_lat REAL, office_lng REAL,
  planned_km REAL, n_employees INTEGER,
  delay_reason TEXT, driver_fault INTEGER,
  PRIMARY KEY (trip_id, bunit_id)
);
CREATE INDEX IF NOT EXISTS ix_trips_scope ON trips(bunit_id, office, trip_direction, day);
CREATE INDEX IF NOT EXISTS ix_trips_cab   ON trips(cab_reg);
CREATE INDEX IF NOT EXISTS ix_trips_day   ON trips(day);

CREATE TABLE IF NOT EXISTS cab_profiles (
  cab_reg TEXT, bunit_id TEXT, office TEXT,
  n_trips INTEGER, n_login_first INTEGER,
  garage_lat REAL, garage_lng REAL,          -- median first-LOGIN-of-day pickup
  driver_fault_rate REAL, eff_vendor TEXT, capacity INTEGER,
  PRIMARY KEY (cab_reg, bunit_id, office)
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")   # sync writes while the service reads
    c.executescript(DDL)
    return c


def geo(s):
    try:
        a, b = str(s).split(",")
        return float(a), float(b)
    except Exception:
        return None, None


def clean(s):
    """Metabase emits the literal strings 'null'/'None' for empty cells, which
    slip past `IS NOT NULL` checks and silently poison downstream filters."""
    if s is None:
        return None
    s = str(s).strip()
    return None if s.lower() in ("", "null", "none", "na", "n/a") else s


def ts(s):
    """Normalise a timestamp to seconds precision.

    Redshift returns variable-precision fractional seconds — '...:45.5',
    '...:21.26', '...:52.813'. Python's fromisoformat only accepts exactly 3 or
    6 fractional digits, so ~12% of rows were failing to parse and being dropped
    from analyses without any error. Truncating at the '.' removes the class of
    bug entirely; we never need sub-second precision."""
    s = clean(s)
    if not s:
        return None
    return s.split(".")[0].replace("Z", "")


def fetch_day(c, day, retries=3):
    """One day of trips as CSV rows. The proxy returns HTTP 200 with a JSON
    error body on failure, so check the body — not just the status."""
    url = "%s?%s" % (c["APPS_SCRIPT_EXEC_URL"], urllib.parse.urlencode({
        "route": "card", "id": c["METABASE_CARD_ID"], "token": c["APPS_SCRIPT_TOKEN"],
        "start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat()}))
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=420) as r:
                body = r.read().decode("utf-8", "replace")
            if body.lstrip().startswith("{"):          # JSON == error envelope
                raise RuntimeError(body[:300])
            return list(csv.DictReader(io.StringIO(body)))
        except Exception as e:
            if attempt == retries - 1:
                raise
            print("   retry %d after %s" % (attempt + 1, str(e)[:120]))
            time.sleep(5 * (attempt + 1))


def upsert(db, rows, day):
    db.execute("DELETE FROM trips WHERE day = ?", (day.isoformat(),))   # idempotent re-pull
    out = []
    for r in rows:
        d = (r.get("trip_direction") or "").upper()
        alat, alng = geo(r.get("anchor_pickup_geo") if d == "LOGIN" else r.get("anchor_drop_geo"))
        if alat is None:
            continue
        olat, olng = geo(r.get("office_geo"))
        num = lambda k: (float(r[k]) if (r.get(k) or "").strip() not in ("", "None") else None)
        out.append((
            r["trip_id"], r.get("bunit_id"), day.isoformat(), clean(r.get("office")),
            clean(r.get("shift")), d,
            ts(r.get("planned_start_time")), ts(r.get("planned_end_time")),
            ts(r.get("cab_allocation_time")),
            clean(r.get("actual_cab_registration")), clean(r.get("mis_driver_id")),
            clean(r.get("vendor_id")), clean(r.get("subvendor_name")),
            (clean(r.get("subvendor_name")) or clean(r.get("vendor_id")) or "NA"),
            int(num("actual_cab_capacity") or 0), int(num("desired_cab_capacity") or 0),
            r.get("cabtype"), alat, alng, olat, olng,
            num("planned_km"), int(num("n_employees") or 0),
            r.get("delay_reason"), 1 if r.get("delay_reason") == "DRIVER" else 0))
    db.executemany("INSERT OR REPLACE INTO trips VALUES (%s)" % ",".join("?" * 25), out)
    db.commit()
    return len(out)


def prune(db, window):
    cut = (date.today() - timedelta(days=window)).isoformat()
    n = db.execute("DELETE FROM trips WHERE day < ?", (cut,)).rowcount
    db.commit()
    return n


def rebuild_profiles(db):
    """Garage anchor = median pickup of the cab's FIRST LOGIN trip of each day.
    MDS exposes a garageLocation field but never populates it, so we derive it."""
    db.execute("DELETE FROM cab_profiles")
    db.execute("""
      INSERT INTO cab_profiles
      WITH firsts AS (
        SELECT cab_reg, bunit_id, office, day, anchor_lat, anchor_lng,
               ROW_NUMBER() OVER (PARTITION BY cab_reg, day ORDER BY planned_start_time) rn
        FROM trips WHERE trip_direction='LOGIN'
      ),
      garage AS (
        SELECT cab_reg, bunit_id, office, COUNT(*) nf,
               AVG(anchor_lat) glat, AVG(anchor_lng) glng
        FROM firsts WHERE rn=1 GROUP BY cab_reg, bunit_id, office
      )
      SELECT t.cab_reg, t.bunit_id, t.office, COUNT(*),
             COALESCE(g.nf,0), g.glat, g.glng,
             AVG(t.driver_fault),
             MAX(t.eff_vendor), MAX(t.capacity)
      FROM trips t
      LEFT JOIN garage g
        ON g.cab_reg=t.cab_reg AND g.bunit_id=t.bunit_id AND g.office=t.office
      GROUP BY t.cab_reg, t.bunit_id, t.office
    """)
    db.commit()
    return db.execute("SELECT COUNT(*) FROM cab_profiles").fetchone()[0]


def stamp(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, str(v)))
    db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="seed the whole window")
    ap.add_argument("--day", help="re-pull one day, YYYY-MM-DD")
    ap.add_argument("--force", action="store_true",
                    help="with --backfill, re-pull days that already have rows")
    ap.add_argument("--ensure", action="store_true",
                    help="backfill only if the window is incomplete; safe to run on every boot")
    a = ap.parse_args()
    if a.ensure:
        a.backfill = True
    c = cfg(); window = int(c.get("HISTORY_WINDOW_DAYS", 30)); db = conn()

    if a.day:
        days = [datetime.strptime(a.day, "%Y-%m-%d").date()]
    elif a.backfill:
        days = [date.today() - timedelta(days=i) for i in range(window, 0, -1)]
        if not a.force:      # resume: a long backfill can be interrupted
            have = {r[0] for r in db.execute("SELECT DISTINCT day FROM trips")}
            skipped = [d for d in days if d.isoformat() in have]
            days = [d for d in days if d.isoformat() not in have]
            if skipped:
                print("skipping %d day(s) already present (use --force to re-pull)"
                      % len(skipped), flush=True)
    else:
        days = [date.today() - timedelta(days=1)]

    total = 0
    for d in days:
        t0 = time.time()
        try:
            rows = fetch_day(c, d)
            n = upsert(db, rows, d); total += n
            print("  %s  %5d rows  %.1fs" % (d, n, time.time() - t0), flush=True)
        except Exception as e:
            print("  %s  FAILED: %s" % (d, str(e)[:200]), flush=True)

    pruned = prune(db, window)
    profiles = rebuild_profiles(db)
    kept = db.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
    span = db.execute("SELECT MIN(day), MAX(day) FROM trips").fetchone()
    stamp(db, "last_sync", datetime.now().isoformat(timespec="seconds"))
    stamp(db, "window_days", window)
    print("\ninserted %d | pruned %d | trips in window %d (%s..%s) | cab profiles %d"
          % (total, pruned, kept, span[0], span[1], profiles))
    print("db: %s (%.1f MB)" % (DB, os.path.getsize(DB) / 1e6))


if __name__ == "__main__":
    main()
