#!/usr/bin/env python3
"""
Replay the shadow log with HISTORY-BASED candidates — the honest test of the
pipeline the tool should have had from the start:

    candidates = cabs with history at this site/direction   (NOT the MDS pool)
    filter     = capacity + vendor + not-used-in-wave
    rank       = the shipped scorer (recommend.score_pool, tod+duty)
    compare    = the cab the deployer actually assigned

Every trip here was predicted BEFORE assignment (the sweep only logs open
trips), and the history window ends before the trip's day, so nothing leaks.

This is deliberately a LOWER BOUND on the new pipeline: real-time feasibility
is NOT applied to competitors (we can't reconstruct every cab's live schedule
after the fact), so busy cabs stay in and push the true pick down. With the
feasibility filter in production the ranks can only improve.

    python3 replay_shadow.py
"""
import gzip, json, glob, re, sqlite3, collections
from datetime import datetime, timedelta

import recommend
from recommend import score_pool

DB = "data/history.db"
WINDOW = 30


# ── vendor matching ──────────────────────────────────────────────────────
# The MDS trip vendor is the MASTER vendor. History's eff_vendor is the
# SUBVENDOR (one master spans several: MIS-ONE -> DCO MIS, Sohith Sohan, EV
# ZIP...), so matching on it excluded the deployer's own pick 58 times out of
# 102 in the first replay. History's vendor_id column IS the master vendor in
# MDS's own vocabulary — the only wrinkle is case ('MIS-One' and 'MIS-ONE' both
# occur), so compare case-folded and nothing fancier.
def vendor_match(mds_v, hist_v):
    return str(mds_v or '').casefold().strip() == str(hist_v or '').casefold().strip()


def load_jsonl(pattern):
    out = []
    for f in sorted(glob.glob(pattern)):
        for l in gzip.open(f).read().decode().strip().split("\n"):
            if l:
                out.append(json.loads(l))
    return out


def main():
    preds = {}
    for p in load_jsonl("shadow/*.jsonl.gz"):
        if "result" in json.dumps(p)[:0]:
            pass
    for f in sorted(glob.glob("shadow/*.jsonl.gz")):
        if ".result." in f:
            continue
        for l in gzip.open(f).read().decode().strip().split("\n"):
            if not l:
                continue
            p = json.loads(l)
            k = (p["buid"], p["trip_id"])
            if k not in preds or p["ts"] > preds[k]["ts"]:
                preds[k] = p

    results = []
    for f in sorted(glob.glob("shadow/*.result.jsonl.gz")):
        for l in gzip.open(f).read().decode().strip().split("\n"):
            if l:
                results.append(json.loads(l))

    db = sqlite3.connect(DB)
    scored = []
    cov_hist = cov_filtered = 0
    vendor_excluded_chosen = 0
    tally_old = collections.Counter()
    tally_new = collections.Counter()

    for r in results:
        if r.get("vendor_changed") or r.get("rank") == "?" or not r.get("chosen"):
            continue
        p = preds.get((r["buid"], r["trip_id"]))
        if not p:
            continue
        day = r["day"]
        lo = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=WINDOW)).strftime("%Y-%m-%d")
        direction = "LOGIN" if r["direction"] == "IN" else "LOGOUT"

        # candidates: every cab seen at this site+direction in the window,
        # with its most recent vendor and capacity
        rows = db.execute(
            """SELECT TRIM(cab_reg), vendor_id, capacity, day FROM trips
               WHERE bunit_id=? AND office=? AND trip_direction=?
                 AND day<? AND day>=? AND cab_reg IS NOT NULL""",
            (r["buid"], p["office"], direction, day, lo)).fetchall()
        if not rows:
            continue
        latest = {}
        for cab, ven, cap, d in rows:
            if cab not in latest or d > latest[cab][2]:
                latest[cab] = (ven, cap, d)

        chosen = r["chosen"]
        in_hist = chosen in latest
        if in_hist:
            cov_hist += 1

        wave_used = set(p.get("wave_assigned") or [])
        want_cap = p.get("capacity")

        pool = []
        chosen_vendor_excluded = False
        for cab, (ven, cap, _d) in latest.items():
            if want_cap and cap and cap != want_cap:
                continue
            if p.get("vendor") and not vendor_match(p["vendor"], ven):
                if cab == chosen:
                    chosen_vendor_excluded = True
                continue
            pool.append({
                "cabRegNo": cab, "capacity": want_cap or cap,
                "cabActive": True, "virtual": False, "busyVehicle": False,
                "complianceStatus": "Compliant", "emptyLegInMetres": None,
                "subVendorName": ven,
            })
        if chosen_vendor_excluded:
            vendor_excluded_chosen += 1
        if any(c["cabRegNo"] == chosen for c in pool):
            cov_filtered += 1

        trip = {
            "tripDirection": r["direction"],
            "tripStartGeoCord": p["trip_attrs"].get("startGeo"),
            "tripEndGeoCord": p["trip_attrs"].get("endGeo"),
            "shiftTime": p["trip_attrs"]["shiftTime"],
            "tripStartTime": p["trip_attrs"]["start"],
            "tripEndTime": p["trip_attrs"]["end"],
            "plannedCabCapacity": want_cap,
        }
        hist = recommend.History(r["buid"], p["office"], direction)
        res = score_pool(trip, pool, hist, wave_assigned=wave_used,
                         chains={}, topn=10)
        order = [x["cab"] for x in res[0]]
        # rank within the FULL ordering, not just top-10
        full = sorted(
            ((x["tier"], -x["exact_score"], -x["kernel"], x["fault_rate"], x["cab"])
             for x in []), )
        rank = order.index(chosen) + 1 if chosen in order else None
        if rank is None:
            # look past top-10: rescore with big topn to get the true rank
            res_all = score_pool(trip, pool, hist, wave_assigned=wave_used,
                                 chains={}, topn=100000)
            order_all = [x["cab"] for x in res_all[0]]
            rank = order_all.index(chosen) + 1 if chosen in order_all else None

        scored.append({
            "buid": r["buid"], "office": p["office"], "shift": r["shift"],
            "direction": r["direction"], "chosen": chosen,
            "old_rank": r.get("rank"),          # pool-based, as logged
            "new_rank": rank,                   # history-based, this replay
            "candidates": len(pool),
            "in_history": in_hist,
        })
        for k in (1, 3, 5, 10):
            if r.get("rank") and r["rank"] <= k:
                tally_old[k] += 1
            if rank and rank <= k:
                tally_new[k] += 1

    n = len(scored)
    if not n:
        print("nothing to replay"); return
    print(f"REPLAY — same {n} deployer decisions, two candidate strategies\n")
    print(f"coverage: deployer's cab had history at the site: {cov_hist}/{n} "
          f"({100*cov_hist/n:.0f}%)")
    print(f"          survived capacity+vendor filters:       {cov_filtered}/{n} "
          f"({100*cov_filtered/n:.0f}%)")
    print(f"          excluded by the VENDOR fuzzy match:     {vendor_excluded_chosen}  "
          f"<- mapping errors, fixable\n")
    med = sorted(s["candidates"] for s in scored)[n // 2]
    print(f"median candidates: {med} (history)   — no feasibility applied, so this is a LOWER bound\n")
    print(f"{'':14s}{'top-1':>8s}{'top-3':>8s}{'top-5':>8s}{'top-10':>8s}")
    print(f"{'OLD (MDS pool)':14s}" + "".join(f"{100*tally_old[k]/n:7.1f}%" for k in (1, 3, 5, 10)))
    print(f"{'NEW (history)':14s}" + "".join(f"{100*tally_new[k]/n:7.1f}%" for k in (1, 3, 5, 10)))

    # where the new pipeline still misses badly
    miss = [s for s in scored if not s["new_rank"] or s["new_rank"] > 10]
    print(f"\n{len(miss)} still outside top-10 with history candidates:")
    why = collections.Counter()
    for s in miss:
        why["no history at site (new/rotated cab)" if not s["in_history"]
            else "has history, ranked low"] += 1
    for k, v in why.most_common():
        print(f"   {v:3d}  {k}")

    by_shift = collections.defaultdict(list)
    for s in scored:
        by_shift[s["shift"]].append(s)
    print(f"\nBY SHIFT (5+ trips):")
    for sh, ss in sorted(by_shift.items()):
        if len(ss) < 5:
            continue
        h5 = sum(1 for x in ss if x["new_rank"] and x["new_rank"] <= 5)
        o5 = sum(1 for x in ss if x["old_rank"] and x["old_rank"] <= 5)
        print(f"   {sh}  n={len(ss):3d}   old top-5 {100*o5/len(ss):5.1f}%   "
              f"new top-5 {100*h5/len(ss):5.1f}%")


if __name__ == "__main__":
    main()
