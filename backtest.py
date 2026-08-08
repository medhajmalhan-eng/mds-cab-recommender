#!/usr/bin/env python3
"""
How often does our #1 match what the deployer actually did?

Runs entirely on the synced history. This is the honest measurement;
evaluate.py (which uses the live pool) is inflated because cabs deployed after
the trip have already been filtered out.

CORRECTED 2026-08-08: candidates used to be "cabs that worked that day at this
site" — same-day knowledge nobody has at prediction time, and an average of 52
candidates where the live problem has ~146. That inflated every number this
script ever printed (26.6% top-1 claimed; 10.5% measured live). Candidates now
come from the PRIOR window only, exactly like the shipped pipeline: every cab
seen at the site+direction in the past 30 days, vendor and capacity from its
most recent trip, master vendor_id matching.

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
    p.add_argument("--sv-filter", action="store_true",
                   help="filter candidates to the trip's SUBVENDOR (the deployer's real "
                        "workflow: subvendor is assigned to the trip first, cab second — "
                        "live waves show open trips already carrying their SV). CAVEAT: in "
                        "this offline test the trip's subvendor comes from the final record, "
                        "so treat the result as an upper bound on the live gain.")
    p.add_argument("--sv-w", type=float, default=0.0,
                   help="subvendor-affinity weight (0 = off). The MASTER vendor is a "
                        "client-facing placeholder; the operating unit is the SUBVENDOR, "
                        "and route knowledge lives there. This boosts cabs whose subvendor "
                        "dominates trips like this one, so a cab with thin personal history "
                        "inherits its subvendor's route record.")
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
                """SELECT anchor_lat,anchor_lng,shift,vendor_id,cab_reg,day,capacity,eff_vendor
                   FROM trips WHERE bunit_id=? AND office=? AND trip_direction=?
                     AND day<? AND day>=? AND anchor_lat IS NOT NULL""",
                (a.buid, office, direction, day.isoformat(), lo)).fetchall()
            if len(hist) < 200:
                continue
            by_cab = defaultdict(list)
            # vendor + capacity per cab from its MOST RECENT prior trip — the
            # same rule the shipped shards use. Nothing from the trip's own day.
            cab_ven, cab_cap, cab_seen, cab_sv = {}, {}, {}, {}
            sv_rows = []          # (lat, lng, shift, age, subvendor) for affinity
            for lat, lng, sh, ven, cab, d, cap, sv in hist:
                cab = (cab or "").strip()
                age = (day - datetime.strptime(d, "%Y-%m-%d").date()).days
                by_cab[cab].append((lat, lng, sh, ven, age))
                sv_rows.append((lat, lng, sh, age, sv))
                if cab not in cab_seen or d > cab_seen[cab]:
                    cab_seen[cab] = d
                    cab_ven[cab] = (ven or "").casefold().strip()
                    cab_cap[cab] = cap
                    cab_sv[cab] = sv

            q = """SELECT trip_id,shift,vendor_id,cab_reg,anchor_lat,anchor_lng,capacity,
                          planned_start_time,planned_end_time,cab_allocation_time,subvendor_name
                   FROM trips WHERE bunit_id=? AND office=? AND trip_direction=? AND day=?
                     AND anchor_lat IS NOT NULL AND cab_reg IS NOT NULL"""
            args = [a.buid, office, direction, day.isoformat()]
            if a.shift:
                q += " AND shift=?"; args.append(a.shift)
            trips = db.execute(q + " ORDER BY cab_allocation_time", args).fetchall()
            if not trips:
                continue

            # candidates = cabs with prior history here. NOT the same-day
            # active set: that is knowledge nobody has at prediction time.
            active = set(by_cab.keys())
            sched = defaultdict(list)      # cab -> [(start,end)] as we assign
            wave_used = defaultdict(set)   # (shift) -> cabs

            for tid, sh, ven, truth, tlat, tlng, cap, ps, pe, alloc, trip_sv in trips:
                truth = (truth or "").strip()
                # SUBVENDOR AFFINITY for this trip: which operating unit runs
                # trips like this one? Same kernel as cab scoring, aggregated per
                # subvendor and normalised to [0,1] against the strongest.
                sv_aff = {}
                if a.sv_w > 0:
                    sh_m0 = mins(sh)
                    agg = defaultdict(float)
                    for lat, lng, hsh, age, sv in sv_rows:
                        d = km(tlat, tlng, lat, lng)
                        if d > CAP_KM:
                            continue
                        hm = mins(hsh)
                        sim = (0.0 if (hm is None or sh_m0 is None)
                               else math.exp(-circ(hm, sh_m0) / TOD_TAU))
                        agg[sv] += math.exp(-d / KERNEL_KM) * (1 + SHIFT_BONUS * sim) * 0.5 ** (age / HALFLIFE)
                    peak = max(agg.values(), default=0.0)
                    if peak > 0:
                        sv_aff = {sv: v / peak for sv, v in agg.items()}
                scored = []
                for cab in active:
                    if cab in wave_used[sh]:
                        continue
                    if a.sv_filter and trip_sv:
                        # subvendor known for the trip -> candidates are that
                        # subvendor's cabs, exactly as the deployer works
                        if cab_sv.get(cab) != trip_sv:
                            continue
                    elif not a.cross_vendor and cab_ven.get(cab) != (ven or "").casefold().strip():
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
                    if a.sv_w > 0:
                        k *= (1 + a.sv_w * sv_aff.get(cab_sv.get(cab), 0.0))
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
