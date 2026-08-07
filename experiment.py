#!/usr/bin/env python3
"""
Test scoring changes against what deployers actually did.

    python3 experiment.py --days 14 --offices 4          compare all variants
    python3 experiment.py --days 14 --sweep tau          tune the time kernel
    python3 experiment.py --days 30 --offices 6 --variant tod

Same discipline as backtest.py: history strictly BEFORE each trip's day, no
live pool, the full set of competing cabs present. Any number here is directly
comparable to the 26.6/45.5/55.2 baseline.

WHY THESE THREE
---------------
From reconciling a real 02:30 wave at Ivy on 2026-08-08, where we went 0-for-3:

  tod   The shift-match bonus (x3, the strongest term) is exact STRING equality.
        02:30 has 8 trips in 30 days at that site, so the term is dead for the
        whole wave — nothing scored `strong`, every card said "no exact match",
        and ranking collapsed to daytime area familiarity. A cab that runs 03:30
        should count for something at 02:30. Graded proximity instead.

  duty  All three cabs the deployer chose were night regulars (one had 20 trips
        at 21:45 and 11 at 05:45). "Does this cab work this hour at all" is a
        signal independent of where it works, and we had no term for it.

  wave  Our #1 for trip 1137525 WAS the deployer's pick — they used it on 1137390,
        the next trip in the same wave. Per-trip scoring counts that as a miss.
        Within one wave, at one shift, the trip<->cab pairing is close to
        interchangeable, so assignment should be solved per wave, not per trip.
        (This is NOT the global greedy that tested worse at 20.4% vs 24.2% —
        that ran across a whole day, where a cab legitimately does 3-5 trips.
        One-cab-per-wave is validated at 99.87%, so within a wave it is a real
        constraint and a real matching problem.)

METRICS
-------
  trip-level top-k   did OUR ranking for THIS trip contain the deployer's cab
  wave-level set     of the cabs actually used in a wave, how many did we name
                     anywhere in the wave — the question "did we identify the
                     right cabs for tonight" rather than "did we guess the
                     deployer's arbitrary pairing"
"""
import argparse, math, os, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "history.db")
KM_LAT, KM_LNG = 111.0, 105.9
EXACT_KM, KERNEL_KM, CAP_KM = 1.0, 3.0, 10.0
SHIFT_BONUS, HALFLIFE, SPEC = 2.0, 21.0, 0.5


def km(a, b, c, d):
    return math.hypot((a - c) * KM_LAT, (b - d) * KM_LNG)


def mins(hhmm):
    """'02:30' -> 150. None for anything unparseable — the extract carries the
    literal string 'null' for missing shifts."""
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def circ_delta(a, b):
    """Minutes between two times of day, the short way round the clock.
    23:45 and 00:15 are 30 minutes apart, not 1410 — and night waves are
    exactly where this matters."""
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


class Cfg:
    """One scoring variant."""
    def __init__(self, name, tod=False, duty=False, wave=False, contest=False,
                 tau=60.0, tier_min=0, duty_w=1.0, duty_tau=90.0, alpha=1.0):
        self.name, self.tod, self.duty, self.wave = name, tod, duty, wave
        self.contest = contest       # opportunity-cost re-ranking, see below
        self.tau, self.tier_min = tau, tier_min
        self.duty_w, self.duty_tau = duty_w, duty_tau
        self.alpha = alpha

    def shift_sim(self, hsh_m, sh_m):
        """1.0 on an exact match either way, so `tod` strictly generalises the
        baseline rather than replacing it."""
        if hsh_m is None or sh_m is None:
            return 0.0
        if not self.tod:
            return 1.0 if hsh_m == sh_m else 0.0
        return math.exp(-circ_delta(hsh_m, sh_m) / self.tau)

    def is_tier1(self, d, hsh_m, sh_m):
        if d > EXACT_KM or hsh_m is None or sh_m is None:
            return False
        return circ_delta(hsh_m, sh_m) <= self.tier_min


def score_cab(cfg, rows, tlat, tlng, sh_m):
    """(tier, exact_weight, kernel) for one cab against one trip."""
    ex_w = kern = 0.0
    n_ex = 0
    for lat, lng, hsh_m, age in rows:
        d = km(tlat, tlng, lat, lng)
        if d > CAP_KM:
            continue
        r = 0.5 ** (age / HALFLIFE)
        sim = cfg.shift_sim(hsh_m, sh_m)
        kern += math.exp(-d / KERNEL_KM) * (1 + SHIFT_BONUS * sim) * r
        if cfg.is_tier1(d, hsh_m, sh_m):
            n_ex += 1
            ex_w += r
    kern /= max(len(rows), 1) ** SPEC

    if cfg.duty:
        # Time-of-day affinity with NO distance restriction: "does this cab work
        # this hour at all", which is what distinguishes a night regular from a
        # day cab that happens to live near the pickup.
        aff = 0.0
        for _lat, _lng, hsh_m, age in rows:
            if hsh_m is None or sh_m is None:
                continue
            aff += math.exp(-circ_delta(hsh_m, sh_m) / cfg.duty_tau) * 0.5 ** (age / HALFLIFE)
        aff /= max(len(rows), 1)
        kern *= (1 + cfg.duty_w * aff)

    return (0 if n_ex else 1, -ex_w, -kern)


def run(db, cfg, buid, offices, dirs, days, window):
    trip_hits = {1: 0, 3: 0, 5: 0}
    n_trips = 0
    set_hit = set_tot = 0
    per_shift = defaultdict(lambda: {"n": 0, 1: 0, 5: 0})

    for day in days:
        for office in offices:
            for direction in dirs:
                lo = (day - timedelta(days=window)).isoformat()
                hist = db.execute(
                    """SELECT anchor_lat,anchor_lng,shift,cab_reg,day
                       FROM trips WHERE bunit_id=? AND office=? AND trip_direction=?
                         AND day<? AND day>=? AND anchor_lat IS NOT NULL
                         AND cab_reg IS NOT NULL""",
                    (buid, office, direction, day.isoformat(), lo)).fetchall()
                if len(hist) < 200:
                    continue
                by_cab = defaultdict(list)
                for lat, lng, sh, cab, d in hist:
                    age = (day - datetime.strptime(d, "%Y-%m-%d").date()).days
                    by_cab[cab.strip()].append((lat, lng, mins(sh), age))

                trips = db.execute(
                    """SELECT trip_id,shift,eff_vendor,cab_reg,anchor_lat,anchor_lng,
                              capacity,planned_start_time,planned_end_time
                       FROM trips WHERE bunit_id=? AND office=? AND trip_direction=? AND day=?
                         AND anchor_lat IS NOT NULL AND cab_reg IS NOT NULL
                       ORDER BY cab_allocation_time""",
                    (buid, office, direction, day.isoformat())).fetchall()
                if not trips:
                    continue

                trips = [(t[0], t[1], t[2], t[3].strip(), t[4], t[5], t[6], t[7], t[8])
                         for t in trips]
                active = {t[3] for t in trips}
                cab_ven = {t[3]: t[2] for t in trips}
                cab_cap = {t[3]: t[6] for t in trips}

                waves = defaultdict(list)
                for t in trips:
                    waves[t[1]].append(t)

                for sh, wtrips in waves.items():
                    sh_m = mins(sh)
                    # candidates per trip, under the same hard filters as production
                    cand = {}
                    for t in wtrips:
                        tid, _sh, ven, truth, tlat, tlng, cap, ps, pe = t
                        c = []
                        for cab in active:
                            if cab_ven.get(cab) != ven or cab_cap.get(cab) != cap:
                                continue
                            rows = by_cab.get(cab)
                            if not rows:
                                continue
                            c.append((cab, score_cab(cfg, rows, tlat, tlng, sh_m)))
                        c.sort(key=lambda x: x[1])
                        cand[tid] = c

                    if cfg.contest and len(wtrips) > 1:
                        cand = contest_rerank(cand, cfg.alpha)

                    truth_set = {t[3] for t in wtrips}

                    if cfg.wave:
                        # Global greedy within the wave: take the best (trip, cab)
                        # pair anywhere in the wave, lock both, repeat. One cab
                        # per trip and one trip per cab, which is the real
                        # constraint (validated 99.87%).
                        pairs = []
                        for tid, cl in cand.items():
                            for cab, sc in cl:
                                pairs.append((sc, tid, cab))
                        pairs.sort()
                        used_t, used_c, assign = set(), set(), {}
                        for sc, tid, cab in pairs:
                            if tid in used_t or cab in used_c:
                                continue
                            assign[tid] = cab
                            used_t.add(tid); used_c.add(cab)
                        pred_set = set(assign.values())
                        # top-k still comes from the per-trip ranking, minus cabs
                        # the matching gave to an earlier trip
                        for t in wtrips:
                            tid, truth = t[0], t[3]
                            others = {c for x, c in assign.items() if x != tid}
                            order = [c for c, _ in cand[tid] if c not in others]
                            rank = order.index(truth) + 1 if truth in order else None
                            n_trips += 1
                            per_shift[sh]["n"] += 1
                            for k in (1, 3, 5):
                                if rank and rank <= k:
                                    trip_hits[k] += 1
                                    if k in (1, 5):
                                        per_shift[sh][k] += 1
                    else:
                        # Sequential, in the deployer's own allocation order:
                        # once a cab is used in this wave it is out.
                        used = set()
                        pred_set = set()
                        for t in wtrips:
                            tid, _sh, _v, truth, *_ = t
                            order = [c for c, _ in cand[tid] if c not in used]
                            rank = order.index(truth) + 1 if truth in order else None
                            n_trips += 1
                            per_shift[sh]["n"] += 1
                            for k in (1, 3, 5):
                                if rank and rank <= k:
                                    trip_hits[k] += 1
                                    if k in (1, 5):
                                        per_shift[sh][k] += 1
                            if order:
                                pred_set.add(order[0])
                            used.add(truth)

                    set_hit += len(pred_set & truth_set)
                    set_tot += len(truth_set)

    return {"n": n_trips, "trip": trip_hits,
            "set_hit": set_hit, "set_tot": set_tot, "per_shift": per_shift}


def scalar(sc):
    """Collapse the (tier, -exact, -kernel) sort key into one number, preserving
    the ordering. Needed only by `contest`, which does arithmetic on scores."""
    tier, neg_ex, neg_kern = sc
    return (1 - tier) * 1000.0 + (-neg_ex) * 10.0 + (-neg_kern)


def contest_rerank(cand, alpha):
    """Re-rank each trip's candidates by opportunity cost.

    A cab that is the runaway best choice for ANOTHER trip in this wave is a
    poor pick here, even if it also tops this trip's list — taking it forces the
    other trip onto something worse. Deployers do this implicitly; the scorer
    had no notion of it, which is how our #1 for trip 1137525 was a cab the
    deployer spent on 1137390 instead.

    Unlike full wave matching this keeps a complete ranking per trip, so the
    deployer still clicks a trip and sees five cabs — the UI does not change.
    """
    # Top TWO scores per cab, not just the best: "max over OTHER trips" is the
    # best score when this trip is not the one holding it, and the runner-up
    # when it is. Keeping both makes the whole pass linear instead of rescanning
    # the wave for every cab that happens to top a list.
    best = defaultdict(lambda: [(-1.0, None), (-1.0, None)])   # cab -> [(score,tid) x2]
    vals = {}
    for tid, cl in cand.items():
        for cab, sc in cl:
            v = scalar(sc)
            vals[(tid, cab)] = v
            b = best[cab]
            if v > b[0][0]:
                b[1] = b[0]; b[0] = (v, tid)
            elif v > b[1][0]:
                b[1] = (v, tid)
    out = {}
    for tid, cl in cand.items():
        rescored = []
        for cab, _sc in cl:
            v = vals[(tid, cab)]
            b = best[cab]
            other = b[1][0] if b[0][1] == tid else b[0][0]
            rescored.append((cab, -(v - alpha * max(0.0, other - v))))
        rescored.sort(key=lambda x: x[1])
        out[tid] = rescored
    return out


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--buid", default="ivycomptech-IVYHyd")
    p.add_argument("--offices", type=int, default=3, help="top-N offices by volume")
    p.add_argument("--dir", default="BOTH", choices=["LOGIN", "LOGOUT", "BOTH"])
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--date", help="last day to score (default: yesterday). Use this to "
                                  "evaluate on a period the parameters were NOT tuned on.")
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--variant", default="all")
    p.add_argument("--sweep")
    p.add_argument("--rare", type=int, default=200,
                   help="a shift with fewer than this many trips counts as rare")
    a = p.parse_args()

    db = sqlite3.connect(DB)
    end = (datetime.strptime(a.date, "%Y-%m-%d").date() if a.date
           else datetime.now().date() - timedelta(days=1))
    days = [end - timedelta(days=i) for i in range(a.days - 1, -1, -1)]
    dirs = ["LOGIN", "LOGOUT"] if a.dir == "BOTH" else [a.dir]
    offices = [r[0] for r in db.execute(
        "SELECT office, COUNT(*) n FROM trips WHERE bunit_id=? GROUP BY office "
        "ORDER BY n DESC LIMIT ?", (a.buid, a.offices))]

    print(f"{a.buid} | {len(offices)} office(s): {', '.join(offices)}")
    print(f"{'+'.join(dirs)} | {days[0]}..{days[-1]} | {a.window}d rolling history\n")

    if a.sweep == "tau":
        print(f"{'tau (min)':>10s} {'top-1':>7s} {'top-3':>7s} {'top-5':>7s} {'set':>7s}")
        for tau in (15, 30, 45, 60, 90, 120, 180):
            r = run(db, Cfg(f"tau{tau}", tod=True, tau=tau), a.buid, offices, dirs, days, a.window)
            print(f"{tau:>10d} {pct(r['trip'][1],r['n']):6.1f}% {pct(r['trip'][3],r['n']):6.1f}% "
                  f"{pct(r['trip'][5],r['n']):6.1f}% {pct(r['set_hit'],r['set_tot']):6.1f}%")
        return

    if a.sweep == "duty":
        # A sweep with no baseline row is unreadable — every column looks like a
        # number rather than a delta, and duty_w=0 IS the baseline, so include it.
        print(f"{'duty_w':>8s} {'duty_tau':>9s} {'top-1':>7s} {'top-3':>7s} {'top-5':>7s} {'set':>7s}")
        b = run(db, Cfg("baseline"), a.buid, offices, dirs, days, a.window)
        bb = tuple(pct(b["trip"][k], b["n"]) for k in (1, 3, 5)) + (pct(b["set_hit"], b["set_tot"]),)
        print(f"{'0 (base)':>8s} {'—':>9s} {bb[0]:6.1f}% {bb[1]:6.1f}% {bb[2]:6.1f}% {bb[3]:6.1f}%")
        for w in (1.0, 2.0, 4.0, 8.0, 16.0, 32.0):
            for dt in (60, 120, 240):
                r = run(db, Cfg("d", duty=True, duty_w=w, duty_tau=dt),
                        a.buid, offices, dirs, days, a.window)
                v = tuple(pct(r["trip"][k], r["n"]) for k in (1, 3, 5)) + (pct(r["set_hit"], r["set_tot"]),)
                d = "  ".join(f"{x - y:+.1f}" for x, y in zip(v, bb))
                print(f"{w:>8.1f} {dt:>9d} {v[0]:6.1f}% {v[1]:6.1f}% {v[2]:6.1f}% {v[3]:6.1f}%   {d}")
        return

    if a.sweep == "confirm":
        # Baseline vs the tuned candidate only, on as much data as you can
        # afford. Everything above was chosen on 7 days / 2 offices; a gain that
        # does not survive a wider slice was a gain in the tuning noise.
        variants = [Cfg("baseline"),
                    Cfg("tod+duty (tuned)", tod=True, tau=30,
                        duty=True, duty_w=16.0, duty_tau=60)]
        base = None
        print(f"{'variant':24s} {'top-1':>7s} {'top-3':>7s} {'top-5':>7s} {'wave-set':>9s}   vs baseline")
        print("-" * 78)
        res = {}
        for cfg in variants:
            r = run(db, cfg, a.buid, offices, dirs, days, a.window)
            res[cfg.name] = r
            v = tuple(pct(r["trip"][k], r["n"]) for k in (1, 3, 5)) + (pct(r["set_hit"], r["set_tot"]),)
            if base is None:
                base = v; d = ""
            else:
                d = "  ".join(f"{x - y:+.1f}" for x, y in zip(v, base))
            print(f"{cfg.name:24s} {v[0]:6.1f}% {v[1]:6.1f}% {v[2]:6.1f}% {v[3]:8.1f}%   {d}")
        print(f"\n{res['baseline']['n']} trips scored")
        counts = dict(db.execute(
            "SELECT shift, COUNT(*) FROM trips WHERE bunit_id=? GROUP BY shift", (a.buid,)))
        rare = {s for s, c in counts.items() if c < a.rare}
        print(f"\nRARE SHIFTS ONLY (<{a.rare} trips in 30d):")
        for name, r in res.items():
            n = sum(v["n"] for s, v in r["per_shift"].items() if s in rare)
            h1 = sum(v[1] for s, v in r["per_shift"].items() if s in rare)
            h5 = sum(v[5] for s, v in r["per_shift"].items() if s in rare)
            if n:
                print(f"  {name:24s} n={n:<6d} top-1 {pct(h1,n):5.1f}%   top-5 {pct(h5,n):5.1f}%")
        return

    variants = [
        Cfg("baseline"),
        Cfg("tod  (time proximity)", tod=True, tau=60),
        Cfg("duty (works this hour)", duty=True, duty_w=1.0),
        Cfg("wave (match per wave)", wave=True),
        Cfg("contest a=0.5", contest=True, alpha=0.5),
        Cfg("contest a=1.0", contest=True, alpha=1.0),
        Cfg("tod+duty", tod=True, tau=30, duty=True, duty_w=8.0, duty_tau=120),
        Cfg("tod+duty+contest", tod=True, tau=30, duty=True, duty_w=8.0,
            duty_tau=120, contest=True, alpha=0.5),
        Cfg("tod+duty+wave", tod=True, tau=30, duty=True, duty_w=8.0,
            duty_tau=120, wave=True),
    ]
    if a.variant != "all":
        variants = [v for v in variants if v.name.startswith(a.variant)] or variants[:1]

    base = None
    print(f"{'variant':24s} {'top-1':>7s} {'top-3':>7s} {'top-5':>7s} {'wave-set':>9s}   vs baseline")
    print("-" * 78)
    results = {}
    for cfg in variants:
        r = run(db, cfg, a.buid, offices, dirs, days, a.window)
        results[cfg.name] = r
        t1, t3, t5 = (pct(r["trip"][k], r["n"]) for k in (1, 3, 5))
        st = pct(r["set_hit"], r["set_tot"])
        if base is None:
            base = (t1, t3, t5, st)
            delta = ""
        else:
            delta = "  ".join(f"{x - y:+.1f}" for x, y in zip((t1, t3, t5, st), base))
        print(f"{cfg.name:24s} {t1:6.1f}% {t3:6.1f}% {t5:6.1f}% {st:8.1f}%   {delta}")
    print(f"\n{results[variants[0].name]['n']} trips scored")

    # Rare shifts are the whole point of tod/duty, and they are a small enough
    # slice that an overall average can hide the effect entirely.
    counts = dict(db.execute(
        "SELECT shift, COUNT(*) FROM trips WHERE bunit_id=? GROUP BY shift", (a.buid,)))
    rare = {s for s, c in counts.items() if c < a.rare}
    if rare and len(results) > 1:
        print(f"\nRARE SHIFTS ONLY (<{a.rare} trips in 30d at this BU):")
        print(f"{'variant':24s} {'n':>6s} {'top-1':>7s} {'top-5':>7s}")
        print("-" * 48)
        for name, r in results.items():
            n = sum(v["n"] for s, v in r["per_shift"].items() if s in rare)
            h1 = sum(v[1] for s, v in r["per_shift"].items() if s in rare)
            h5 = sum(v[5] for s, v in r["per_shift"].items() if s in rare)
            if n:
                print(f"{name:24s} {n:>6d} {pct(h1,n):6.1f}% {pct(h5,n):6.1f}%")


if __name__ == "__main__":
    main()
