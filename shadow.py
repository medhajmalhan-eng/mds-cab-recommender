#!/usr/bin/env python3
"""
Shadow logging: record every recommendation, then reconcile it against what the
deployer actually did.

This exists because two things CANNOT be settled offline, and guessing at them
would just bake in a wrong answer:

  1. DEADHEAD vs EXACT-ROUTE. The tier rule (exact-route always outranks the
     fallback kernel) was fitted on history, where emptyLegInMetres does not
     exist. Live, it produces things like "#1 did this route once but is 27 km
     away, #2 has 33 area trips and is 1.9 km away" — and a deployer would very
     likely take #2. Logging both, with the deadhead of every candidate, lets us
     measure which the deployer actually prefers instead of assuming.

  2. ACCEPTANCE. Agreement with history is a proxy. Whether a deployer clicks
     our #1 is the real metric, and only production produces it.

    python3 shadow.py reconcile      match logged recos to actual assignments
    python3 shadow.py report         acceptance + the deadhead answer
"""
import json, os, sqlite3, sys
from datetime import datetime, date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "history.db")

DDL = """
CREATE TABLE IF NOT EXISTS reco_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, buid TEXT, trip_id TEXT, trip_day TEXT,
  layer INTEGER, office TEXT, direction TEXT, shift TEXT, vendor TEXT,
  pool_size INTEGER, eligible INTEGER,
  recs TEXT,                    -- JSON: full top-N incl. deadhead & tier
  chosen_cab TEXT,              -- filled by reconcile
  chosen_rank INTEGER,          -- 1-based; NULL = not in our list
  chosen_deadhead REAL,
  reconciled_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_reco_trip ON reco_log(buid, trip_id, trip_day);
"""


def db():
    # timeout + WAL: request threads log recommendations while the nightly sync
    # holds long write transactions on the same file — without WAL those collide
    # with "database is locked".
    c = sqlite3.connect(DB, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(DDL)
    return c


def log(buid, trip, layer, result, trip_day=None):
    """Called on every /recommend for a still-open trip. Deliberately stores the
    FULL feature set for each candidate — rank alone can't answer why a deployer
    went elsewhere. trip_day is the WAVE date (deployers work tomorrow's waves
    at midnight), not the calendar date of the request."""
    try:
        c = db()
        c.execute("""INSERT INTO reco_log
            (ts,buid,trip_id,trip_day,layer,office,direction,shift,vendor,
             pool_size,eligible,recs)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (datetime.now().isoformat(timespec="seconds"), buid,
                   str(trip["tripId"]), trip_day or date.today().isoformat(), layer,
                   trip.get("officeName"), trip.get("tripDirection"),
                   datetime.fromtimestamp(trip["shiftTime"] / 1000).strftime("%H:%M"),
                   trip.get("vendorName"),
                   result.get("pool_size"), result.get("eligible"),
                   json.dumps([{k: r.get(k) for k in
                                ("cab", "tier", "n_exact", "n_history", "nearest_km",
                                 "deadhead_km", "fault_rate", "kernel", "confidence",
                                 "feasibility", "subvendor")}
                               for r in result.get("recommendations", [])])))
        c.commit(); c.close()
    except Exception as e:            # logging must never break a recommendation
        sys.stderr.write("shadow log failed: %s\n" % e)


def reconcile(buid=None, day=None):
    """Fill in what the deployer actually chose, from the synced trip history."""
    c = db()
    day = day or date.today().isoformat()
    rows = c.execute("""SELECT id, buid, trip_id, trip_day, recs FROM reco_log
                        WHERE chosen_cab IS NULL AND trip_day<=?
                        %s""" % ("AND buid=?" if buid else ""),
                     (day, buid) if buid else (day,)).fetchall()
    n = 0
    for rid, b, tid, tday, recs in rows:
        got = c.execute("SELECT cab_reg FROM trips WHERE trip_id=? AND bunit_id=?",
                        (tid, b)).fetchone()
        if not got or not got[0]:
            continue                   # trip not synced yet, or still unassigned
        cab = got[0]
        lst = json.loads(recs or "[]")
        rank = next((i + 1 for i, r in enumerate(lst) if r["cab"] == cab), None)
        dh = next((r.get("deadhead_km") for r in lst if r["cab"] == cab), None)
        c.execute("""UPDATE reco_log SET chosen_cab=?, chosen_rank=?, chosen_deadhead=?,
                     reconciled_at=? WHERE id=?""",
                  (cab, rank, dh, datetime.now().isoformat(timespec="seconds"), rid))
        n += 1
    c.commit(); c.close()
    return n, len(rows)


def report():
    c = db()
    tot = c.execute("SELECT COUNT(*) FROM reco_log WHERE chosen_cab IS NOT NULL").fetchone()[0]
    if not tot:
        print("nothing reconciled yet — run `python3 shadow.py reconcile` after the "
              "nightly sync has picked up the day's assignments")
        return
    print("reconciled recommendations: %d\n" % tot)
    print("ACCEPTANCE (deployer's pick vs our ranking)")
    for k in (1, 3, 5):
        n = c.execute("SELECT COUNT(*) FROM reco_log WHERE chosen_rank<=?", (k,)).fetchone()[0]
        print("  top-%d  %5.1f%%" % (k, 100 * n / tot))
    miss = c.execute("SELECT COUNT(*) FROM reco_log "
                     "WHERE chosen_cab IS NOT NULL AND chosen_rank IS NULL").fetchone()[0]
    print("  not in our list  %5.1f%%" % (100 * miss / tot))

    # ---- the deadhead question ----
    print("\nDEADHEAD vs EXACT-ROUTE  (does the tier rule match the deployer?)")
    rows = c.execute("SELECT recs, chosen_cab FROM reco_log "
                     "WHERE chosen_cab IS NOT NULL AND layer=1").fetchall()
    conflict = agree = 0
    for recs, cab in rows:
        lst = json.loads(recs or "[]")
        if len(lst) < 2:
            continue
        top, rest = lst[0], lst[1:]
        # a conflict is: our #1 wins on exact-route but is FARTHER than an
        # alternative that has no exact history
        closer = [r for r in rest
                  if r.get("deadhead_km") is not None and top.get("deadhead_km") is not None
                  and r["deadhead_km"] < top["deadhead_km"] - 5 and r["tier"] == 2
                  and top["tier"] == 1]
        if not closer:
            continue
        conflict += 1
        if cab == top["cab"]:
            agree += 1
    if conflict:
        print("  cases where our #1 had exact-route history but was >5 km farther")
        print("  than a closer alternative: %d" % conflict)
        print("  deployer still took our #1: %.1f%%" % (100 * agree / conflict))
        print("  -> if this is low, deadhead should outrank the exact-route tier")
    else:
        print("  no such cases logged yet")
    c.close()


def week_report(days=7):
    """Per-site / per-shift comparison of sweeper predictions vs deployer choices.

    One prediction per trip: the LAST one logged BEFORE cab_allocation_time.
    Anything logged after assignment is discarded — a late 'prediction' knows a
    drained pool and would fake accuracy. Also reports coverage: how many of
    the deployed trips we managed to predict in time at all."""
    c = db()
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = c.execute("""
        SELECT r.buid, r.trip_id, r.direction, r.shift, r.ts, r.recs, r.chosen_cab,
               t.cab_allocation_time
        FROM reco_log r
        JOIN trips t ON t.trip_id = r.trip_id AND t.bunit_id = r.buid
        WHERE r.chosen_cab IS NOT NULL AND r.trip_day >= ?""", (since,)).fetchall()

    best = {}                              # (buid,trip) -> latest valid prediction
    for buid, tid, dirn, shift, ts, recs, chosen, alloc in rows:
        if alloc and ts > alloc:
            continue                       # logged after the deployer acted: invalid
        k = (buid, tid)
        if k not in best or ts > best[k][0]:
            best[k] = (ts, buid, dirn, shift, recs, chosen, alloc)

    def rank_of(recs, cab):
        lst = json.loads(recs or "[]")
        return next((i + 1 for i, r in enumerate(lst) if r["cab"] == cab), None)

    per_site, per_shift, per_cut = {}, {}, {}
    N = 0
    for ts, buid, dirn, shift, recs, chosen, alloc in best.values():
        rk = rank_of(recs, chosen)
        N += 1
        cut_h = 3.0 if dirn == "IN" else 1.0
        band = "?"
        try:                                # was the deployer before their cutoff?
            hh, mm = map(int, shift.split(":"))
            trip_day = alloc[:10] if alloc else None
            sdt = datetime.fromisoformat("%sT%02d:%02d:00" % (trip_day, hh, mm))
            band = ("before cutoff" if datetime.fromisoformat(alloc)
                    <= sdt - timedelta(hours=cut_h) else "after cutoff")
        except Exception:
            pass
        for agg, key in ((per_site, buid), (per_shift, "%s %s" % (dirn, shift)),
                         (per_cut, "%s, %s" % (dirn, band))):
            a = agg.setdefault(key, [0, 0, 0, 0])   # n, top1, top3, top5
            a[0] += 1
            for i, kk in enumerate((1, 3, 5), start=1):
                if rk and rk <= kk:
                    a[i] += 1

    if not N:
        print("nothing to report yet — the sweeper needs to run and a nightly "
              "reconcile must complete first")
        return
    total = c.execute("""SELECT COUNT(DISTINCT trip_id||'|'||bunit_id) FROM trips
                         WHERE day >= ? AND cab_reg IS NOT NULL""", (since,)).fetchone()[0]
    print("WEEK REPORT — %d predicted trips, %d deployed trips in window "
          "(coverage %.0f%%)\n" % (N, total, 100.0 * N / max(total, 1)))

    def show(title, agg, min_n=10):
        print(title)
        print("  %-28s %6s %7s %7s %7s" % ("", "n", "top-1", "top-3", "top-5"))
        for k in sorted(agg, key=lambda x: -agg[x][0]):
            n, t1, t3, t5 = agg[k]
            if n < min_n:
                continue
            print("  %-28s %6d %6.1f%% %6.1f%% %6.1f%%"
                  % (str(k)[:28], n, 100 * t1 / n, 100 * t3 / n, 100 * t5 / n))
        print()

    show("BY CUTOFF", per_cut, min_n=1)
    show("BY SITE", per_site)
    show("BY SHIFT (waves with >=10 predictions)", per_shift)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "reconcile":
        done, seen = reconcile()
        print("reconciled %d of %d pending" % (done, seen))
    elif cmd == "week":
        week_report(int(sys.argv[2]) if len(sys.argv) > 2 else 7)
    else:
        report()
