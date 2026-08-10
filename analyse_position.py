#!/usr/bin/env python3
"""
Does the deployer pick the cab that is PHYSICALLY NEAREST when the trip starts?

The single biggest unexplained loss: on ~1/3 of decisions the chosen cab's whole
history sits 3.5 km from the pickup while our #1 ran that exact route days ago.
Two explanations were already tested and killed — time-of-day mismatch (gap is 0
in every bucket) and the garage anchor (median 9.4 km, no signal). The remaining
one is live position: the deployer knows the cab just dropped nearby.

That is now measurable. For any past day, one trip/filter call returns EVERY
trip at that BU with cab, times and geocodes — so each cab's real chain that day
can be reconstructed, and for each decision we can ask:

    how far was this cab's PREVIOUS DROP from this trip's pickup?

and compare the deployer's cab against ours on the same trips. If theirs is
systematically closer, position is the missing feature and belongs in the score.
If not, the deep misses are driven by something outside any data we hold
(phone calls, rotation, favours) and no scoring change will recover them.

    python3 analyse_position.py
"""
import gzip, json, glob, math, statistics, collections
from datetime import datetime, timedelta

from mds import MDS

KM_LAT, KM_LNG = 111.0, 105.9
NO_PREV = object()          # cab had no earlier trip that day — idle, not "far"


def km(a, b, c, d):
    return math.hypot((a - c) * KM_LAT, (b - d) * KM_LNG)


def geo(s):
    try:
        a, b = str(s).split(",")
        return float(a), float(b)
    except Exception:
        return None


def load():
    preds = {}
    for f in glob.glob("shadow/*.jsonl.gz"):
        if ".result." in f:
            continue
        for l in gzip.open(f).read().decode().strip().split("\n"):
            if not l:
                continue
            p = json.loads(l)
            k = (p["buid"], p["trip_id"])
            if k not in preds or p["ts"] > preds[k]["ts"]:
                preds[k] = p
    res = []
    for f in glob.glob("shadow/*.result.jsonl.gz"):
        for l in gzip.open(f).read().decode().strip().split("\n"):
            if l:
                res.append(json.loads(l))
    return preds, res


def chains_for(m, buid, day):
    """Every cab's trips that day at this BU, sorted — reconstructed from one call."""
    guids = m.vendor_guids(buid)
    day_ms = int(datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)
    out = collections.defaultdict(list)
    for t in m.trips(buid, guids, day_ms):
        cab = (t.get("cabReg") or "").strip()
        if not cab or not t.get("tripStartTime"):
            continue
        out[cab].append({
            "start": t["tripStartTime"] / 1000.0,
            "end": t["tripEndTime"] / 1000.0,
            "drop": geo(t.get("tripEndGeoCord")),
            "pick": geo(t.get("tripStartGeoCord")),
            "trip_id": str(t["tripId"]),
        })
    for c in out:
        out[c].sort(key=lambda a: a["start"])
    return out


def prev_drop_km(chain, trip_start, tlat, tlng, exclude_trip):
    """Distance from the cab's last drop BEFORE this trip to this pickup.
    NO_PREV when the cab had not worked yet — that is 'idle', a different state
    from 'far away', and averaging them together would hide the effect."""
    prev = None
    for a in chain or []:
        if a["trip_id"] == exclude_trip:
            continue
        if a["end"] <= trip_start and (prev is None or a["end"] > prev["end"]):
            prev = a
    if prev is None:
        return NO_PREV
    if not prev["drop"]:
        return None
    return km(tlat, tlng, prev["drop"][0], prev["drop"][1])


def main():
    preds, res = load()
    m = MDS()
    m.login()

    cache = {}
    rows = []
    for r in res:
        if r.get("vendor_changed") or not r.get("chosen"):
            continue
        p = preds.get((r["buid"], r["trip_id"]))
        if not p or not p.get("recs"):
            continue
        direction = "LOGIN" if r["direction"] == "IN" else "LOGOUT"
        g = geo(p["trip_attrs"].get("startGeo") if direction == "LOGIN"
                else p["trip_attrs"].get("endGeo"))
        if not g:
            continue
        key = (r["buid"], r["day"])
        if key not in cache:
            try:
                cache[key] = chains_for(m, r["buid"], r["day"])
            except Exception as e:
                print(f"   {key}: {str(e)[:80]}")
                cache[key] = {}
        ch = cache[key]
        tstart = p["trip_attrs"]["start"] / 1000.0
        ours = p["recs"][0]["cab"]
        rows.append({
            "rank": r.get("rank"),
            "chosen_km": prev_drop_km(ch.get(r["chosen"]), tstart, g[0], g[1], r["trip_id"]),
            "ours_km": prev_drop_km(ch.get(ours), tstart, g[0], g[1], r["trip_id"]),
            "same": ours == r["chosen"],
            "shift": r["shift"], "direction": r["direction"],
        })

    def summarise(label, vals):
        real = [v for v in vals if isinstance(v, float)]
        idle = sum(1 for v in vals if v is NO_PREV)
        if not real:
            print(f"  {label:34s} no positioned cabs ({idle} idle)")
            return
        real.sort()
        print(f"  {label:34s} n={len(real):3d}  median {statistics.median(real):5.1f} km   "
              f"p25 {real[len(real)//4]:5.1f}   within 5 km {100*sum(1 for v in real if v <= 5)/len(real):4.0f}%"
              f"   | idle (no earlier trip): {idle}")

    print(f"\n{len(rows)} decisions with reconstructed same-day chains\n")
    print("DISTANCE FROM THE CAB'S PREVIOUS DROP TO THIS PICKUP")
    deep = [r for r in rows if not r["same"] and (r["rank"] is None or r["rank"] > 10)]
    print("\nOn DEEP MISSES (our #1 != their pick, their cab outside our top-10):")
    summarise("  deployer's cab", [r["chosen_km"] for r in deep])
    summarise("  our #1", [r["ours_km"] for r in deep])

    hit = [r for r in rows if r["rank"] and r["rank"] <= 5]
    print("\nOn HITS (their cab was in our top-5), for reference:")
    summarise("  deployer's cab", [r["chosen_km"] for r in hit])

    print("\nALL decisions:")
    summarise("  deployer's cab", [r["chosen_km"] for r in rows])
    summarise("  our #1", [r["ours_km"] for r in rows])

    # The decisive test: on the same trip, was theirs closer than ours?
    paired = [r for r in deep
              if isinstance(r["chosen_km"], float) and isinstance(r["ours_km"], float)]
    if paired:
        closer = sum(1 for r in paired if r["chosen_km"] < r["ours_km"])
        print(f"\nPAIRED on deep misses (both cabs had a previous trip), n={len(paired)}:")
        print(f"  deployer's cab was CLOSER than ours: {closer}/{len(paired)} "
              f"({100*closer/len(paired):.0f}%)   [50% = no signal]")
        diffs = sorted(r["chosen_km"] - r["ours_km"] for r in paired)
        print(f"  median (theirs - ours): {statistics.median(diffs):+.1f} km "
              f"(negative = they pick nearer cabs)")
    idle_chosen = sum(1 for r in deep if r["chosen_km"] is NO_PREV)
    print(f"\nOn deep misses, deployer picked an IDLE cab (no earlier trip today): "
          f"{idle_chosen}/{len(deep)}")


if __name__ == "__main__":
    main()
