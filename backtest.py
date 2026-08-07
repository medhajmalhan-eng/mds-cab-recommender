#!/usr/bin/env python3
"""
How often does our #1 match what the deployer actually did?

Runs entirely on the synced history — NO live MDS pool — so nothing has been
removed and the full set of competing cabs is present. This is the honest
measurement; evaluate.py (which uses the live pool) is inflated because cabs
deployed after the trip have already been filtered out.

    python3 backtest.py                                  yesterday, all waves
    python3 backtest.py --date 2026-08-05 --dir LOGIN
    python3 backtest.py --office IN-SKY-HYD --shift 09:00 --show 20
    python3 backtest.py --days 7                         last 7 days

Method, per trip, using ONLY data from before that day:
  candidates = cabs that worked that day at that BU+office, same vendor,
               matching capacity, no time conflict, not already used in the wave
  score      = exact tier -> kernel exp(-d/3km) x time-of-day x recency / n^0.5
               x (1 + 16 x duty), duty = does this cab work this hour at all
  trips are walked in cab_allocation_time order, exactly as the deployer worked
"""
import argparse, math, os, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "history.db")
KM_LAT, KM_LNG = 111.0, 105.9
EXACT_KM, KERNEL_KM, CAP_KM = 1.0, 3.0, 10.0
SHIFT_BONUS, HALFLIFE, SPEC = 2.0, 21.0, 0.5
BUFFER_MIN = 30
# Time-of-day terms, added 2026-08-08 — must stay in step with recommend.py or
# this script silently measures a model nobody is running. experiment.py is the
# tool for TRYING changes; this one reports the shipped model.
TOD_TAU, DUTY_W, DUTY_TAU = 30.0, 16.0, 60.0


def mins(hhmm):
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def circ(a, b):
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def km(a, b, c, d):
    return math.hypot((a - c) * KM_LAT, (b - d) * KM_LNG)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--buid", default="ivycomptech-IVYHyd")
    p.add_argument("--office")
    p.add_argument("--dir", default="LOGIN", choices=["LOGIN", "LOGOUT", "BOTH"])
    p.add_argument("--date")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--shift")
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--show", type=int, default=15, help="per-trip lines to print")
    p.add_argument("--cross-vendor", action="store_true", help="Layer 2: drop the vendor filter")
    a = p.parse_args()

    db = sqlite3.connect(DB)
    end = (datetime.strptime(a.date, "%Y-%m-%d").date() if a.date
           else datetime.now().date() - timedelta(days=1))
    days = [end - timedelta(days=i) for i in range(a.days - 1, -1, -1)]
    dirs = ["LOGIN", "LOGOUT"] if a.dir == "BOTH" else [a.dir]

    office = a.office
    if not office:
        r = db.execute("""SELECT office, COUNT(*) n FROM trips WHERE bunit_id=?
                          GROUP BY office ORDER BY n DESC LIMIT 1""", (a.buid,)).fetchone()
        if not r:
            sys.exit("no history for %s — has sync.py run?" % a.buid)
        office = r[0]

    print("%s / %s   %s   %s%s" % (
        a.buid, office, "+".join(dirs),
        days[0].isoformat() if len(days) == 1 else "%s..%s" % (days[0], days[-1]),
        "   [LAYER 2: any vendor]" if a.cross_vendor else ""))
    print("history window: %d days before each trip's day\n" % a.window)

    tot = {1: 0, 3: 0, 5: 0}
    n = shown = 0
    pool_sizes = []
    for day in days:
        for direction in dirs:
            lo = (day - timedelta(days=a.window)).isoformat()
            hist = db.execute(
                """SELECT anchor_lat,anchor_lng,shift,eff_vendor,cab_reg,day
                   FROM trips WHERE bunit_id=? AND office=? AND trip_direction=?
                     AND day<? AND day>=? AND anchor_lat IS NOT NULL""",
                (a.buid, office, direction, day.isoformat(), lo)).fetchall()
            if len(hist) < 200:
                continue
            by_cab = defaultdict(list)
            for lat, lng, sh, ven, cab, d in hist:
                by_cab[cab].append((lat, lng, sh, ven, (day - datetime.strptime(d, "%Y-%m-%d").date()).days))

            q = """SELECT trip_id,shift,eff_vendor,cab_reg,anchor_lat,anchor_lng,capacity,
                          planned_start_time,planned_end_time,cab_allocation_time
                   FROM trips WHERE bunit_id=? AND office=? AND trip_direction=? AND day=?
                     AND anchor_lat IS NOT NULL AND cab_reg IS NOT NULL"""
            args = [a.buid, office, direction, day.isoformat()]
            if a.shift:
                q += " AND shift=?"; args.append(a.shift)
            trips = db.execute(q + " ORDER BY cab_allocation_time", args).fetchall()
            if not trips:
                continue

            # everyone who worked that day at this site = the realistic pool
            active = {t[3] for t in trips}
            cab_ven = {}
            cab_cap = {}
            for t in trips:
                cab_ven[t[3]] = t[2]; cab_cap[t[3]] = t[6]
            sched = defaultdict(list)      # cab -> [(start,end)] as we assign
            wave_used = defaultdict(set)   # (shift) -> cabs

            for tid, sh, ven, truth, tlat, tlng, cap, ps, pe, alloc in trips:
                scored = []
                for cab in active:
                    if cab in wave_used[sh]:
                        continue
                    if not a.cross_vendor and cab_ven.get(cab) != ven:
                        continue
                    if cab_cap.get(cab) != cap:
                        continue
                    if any(not (pe <= s or ps >= e) for s, e in sched[cab]):
                        continue
                    rows = by_cab.get(cab)
                    if not rows:
                        continue
                    ex_w = kern = duty = 0.0
                    n_ex = 0
                    sh_m = mins(sh)
                    for lat, lng, hsh, hven, age in rows:
                        r = 0.5 ** (age / HALFLIFE)
                        hsh_m = mins(hsh)
                        # duty is scored with NO distance filter: "does this cab
                        # work this hour" is a fact about the cab, not the pickup
                        if hsh_m is not None and sh_m is not None:
                            duty += math.exp(-circ(hsh_m, sh_m) / DUTY_TAU) * r
                        d = km(tlat, tlng, lat, lng)
                        if d > CAP_KM:
                            continue
                        sim = (0.0 if (hsh_m is None or sh_m is None)
                               else math.exp(-circ(hsh_m, sh_m) / TOD_TAU))
                        kern += math.exp(-d / KERNEL_KM) * (1 + SHIFT_BONUS * sim) * r
                        if d <= EXACT_KM and hsh_m is not None and hsh_m == sh_m:
                            n_ex += 1; ex_w += r
                    n_rows = len(rows)
                    k = kern / n_rows ** SPEC * (1 + DUTY_W * (duty / n_rows))
                    scored.append((0 if n_ex else 1, -ex_w, -k, cab))
                scored.sort()
                pool_sizes.append(len(scored))
                order = [c[-1] for c in scored]
                rank = order.index(truth) + 1 if truth in order else None
                n += 1
                for k in (1, 3, 5):
                    if rank and rank <= k:
                        tot[k] += 1
                if shown < a.show:
                    shown += 1
                    print("  %-9s %-6s %-15s -> ours %-15s %s" % (
                        tid, sh, truth, order[0] if order else "—",
                        ("#%d" % rank) if rank else "not in candidate set"))
                sched[truth].append((ps, pe))
                wave_used[sh].add(truth)

    if not n:
        sys.exit("\nno trips matched — try --days 7, another --office, or check sync.py has run")
    print("\n  %d trips scored | avg %d candidates each" % (n, sum(pool_sizes) / len(pool_sizes)))
    print("  MATCHED WHAT THE DEPLOYER DID:")
    for k in (1, 3, 5):
        print("    top-%d  %5.1f%%   (%d/%d)" % (k, 100 * tot[k] / n, tot[k], n))


if __name__ == "__main__":
    main()
