#!/usr/bin/env python3
"""
Score a fully-deployed wave against what the deployer actually did.

    python3 evaluate.py                          # biggest deployed LOGIN wave today
    python3 evaluate.py --shift 21:45 --dir IN
    python3 evaluate.py --buid goc-GocHyd --office IN-HYD-SAR1 --limit 40

WHAT THIS CAN AND CANNOT TELL YOU
---------------------------------
It replays each trip through the live recommender and reports where the cab the
deployer chose landed in our ranking.

*** THE HIT RATES THIS PRINTS ARE INFLATED. DO NOT QUOTE THEM AS ACCURACY. ***

The MDS candidate pool is a live snapshot, not a historical one. Cabs deployed
*after* the trip you're checking now show as busy and are filtered out — and
those are exactly the cabs that COMPETED with the chosen one at decision time.
The chosen cab survives (we deliberately un-busy it); its rivals do not. Fewer
competitors mechanically pushes the chosen cab up the ranking.

So this measures "can we still find the cab once most alternatives are gone",
which is a much easier question than the one that matters.

USE IT FOR: reading the evidence sentences and judging whether the picks look
sensible to someone who knows the site. That is genuinely valuable and no
metric substitutes for it.

DO NOT USE IT FOR: accuracy. The honest figures are the offline backtest —
26.6% top-1 / 45.5% top-3 / 55.2% top-5 over 41,203 trips, scored against the
full set of cabs active that day. The only clean live measurement is shadow
mode on genuinely OPEN trips, where nothing has been removed yet.

Prefer a LOGIN wave already deployed but not yet run: least drift, and
first-trip-of-day is the segment history predicts best.
"""
import argparse, base64, json, sys, urllib.request
from datetime import datetime

from mds import load_env

BASE = "http://127.0.0.1:8770"
_pw = load_env().get("UI_PASSWORD", "").strip()


def get(path):
    req = urllib.request.Request(BASE + path)
    if _pw:
        req.add_header("Authorization",
                       "Basic " + base64.b64encode(("eval:" + _pw).encode()).decode())
    return json.load(urllib.request.urlopen(req, timeout=120))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--buid", default="ivycomptech-IVYHyd")
    p.add_argument("--office")
    p.add_argument("--shift")
    p.add_argument("--dir", default="IN", choices=["IN", "OUT"])
    p.add_argument("--layer", type=int)
    p.add_argument("--limit", type=int, default=25)
    a = p.parse_args()

    w = get("/wave?buid=%s" % a.buid)
    trips = [t for t in w["all_trips"] if t["assigned"] and t["cab"]]
    if a.office:
        trips = [t for t in trips if t["office"] == a.office]
    trips = [t for t in trips if t["direction"] == a.dir]
    if a.shift:
        trips = [t for t in trips if t["shift"] == a.shift]
    else:
        waves = {}
        for t in trips:
            waves.setdefault(t["shift"], []).append(t)
        # prefer a wave that hasn't run yet; else the biggest
        now = datetime.now().strftime("%H:%M")
        future = {k: v for k, v in waves.items() if k > now}
        pick = max(future or waves, key=lambda k: len(waves[k]))
        trips = waves[pick]
        a.shift = pick
    if not trips:
        sys.exit("no deployed %s trips found for that filter" % a.dir)

    ran = a.shift <= datetime.now().strftime("%H:%M")
    print("%s  %s %s  (%d deployed trips, checking %d)" % (
        a.buid, a.dir, a.shift, len(trips), min(len(trips), a.limit)))
    if ran:
        print("NOTE: this wave has already run — expect heavy drift, results will\n"
              "      understate real accuracy. Prefer a wave that hasn't run yet.")
    print()
    print("  %-9s %-15s %-15s %-8s %s" % ("TRIP", "DEPLOYER CHOSE", "OUR #1", "RANK", "why / evidence"))
    hits = {1: 0, 3: 0, 5: 0}
    n = miss = nopool = 0
    for t in trips[:a.limit]:
        q = "/recommend?buid=%s&tripId=%s&debug=1&evaluate=1" % (a.buid, t["tripId"])
        if a.layer:
            q += "&layer=%d" % a.layer
        d = get(q)
        act = d.get("actual") or {}
        rank = act.get("rank")
        top = d["recommendations"][0]["cab"] if d["recommendations"] else "—"
        n += 1
        for k in (1, 3, 5):
            if rank and rank <= k:
                hits[k] += 1
        if not rank:
            miss += 1
            if not act.get("in_pool"):
                nopool += 1
        note = (act.get("evidence") or "")[:52] if rank else (act.get("why") or "")[:52]
        print("  %-9s %-15s %-15s %-8s %s" % (
            t["tripId"], t["cab"], top,
            ("#%d" % rank) if rank else "—", note))

    print("\n  %d trips scored   [INFLATED — see header, not a measure of accuracy]" % n)
    for k in (1, 3, 5):
        print("    top-%d  %5.1f%%  (%d)" % (k, 100 * hits[k] / n, hits[k]))
    print("    deployer's cab unrankable: %d  (of which %d never offered by MDS)"
          % (miss, nopool))
    print("\n  Why inflated: cabs deployed after this trip are now 'busy' and were")
    print("  dropped — they are exactly the ones that competed with the chosen cab.")
    print("  Honest numbers (offline backtest, 41,203 trips, full candidate set):")
    print("    top-1 26.6%   top-3 45.5%   top-5 55.2%   — LOGIN first-trip 39.3% top-1")


if __name__ == "__main__":
    main()
