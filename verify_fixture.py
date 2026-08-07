#!/usr/bin/env python3
"""
Capture live MDS trips + pools, score them with recommend.py, and write a
fixture that web/scripts/verify.mjs replays through the ported JS scorer.

    python3 verify_fixture.py [buid] [n_trips] > web/fixture.json

WHY THIS EXISTS
---------------
web/public/scorer.js is a hand port of recommend.py. recommend.py is what was
backtested (41,203 trips, 26.6% top-1); scorer.js is what deployers actually
see. A silent divergence between them — a wrong constant, an off-by-one in the
kernel, a sort key in the wrong direction — would degrade recommendations with
no error anywhere and no way to notice from the screen.

So the two are run on IDENTICAL inputs and required to produce identical output:
same cabs, same order, same scores to 1e-9. Not "similar" — identical. Any real
difference is a bug in one of them.

The fixture pins REAL data on purpose. Synthetic pools would miss exactly the
cases that cost time to get right: -1 deadheads, null geocodes, cabs with no
history, chains that wrap past midnight, the 490 km Pune cabs.
"""
import json, sys
from datetime import datetime, date

from mds import MDS
from recommend import History, score_pool

BUID = sys.argv[1] if len(sys.argv) > 1 else "ivycomptech-IVYHyd"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 12


def geo(s):
    try:
        a, b = str(s).split(",")
        return float(a), float(b)
    except Exception:
        return None, None


def web_trip(t):
    """The trimmed shape netlify/functions/wave.mjs emits."""
    return {
        "tripId": t["tripId"], "tripGuid": t["tripGuid"],
        "direction": t["tripDirection"], "shiftTime": t["shiftTime"],
        "office": t["officeName"], "vendor": t.get("vendorName"),
        "capacity": t.get("plannedCabCapacity"), "cutOff": t.get("assignmentCutOffTime"),
        "start": t["tripStartTime"], "end": t["tripEndTime"],
        "startGeo": t.get("tripStartGeoCord"), "endGeo": t.get("tripEndGeoCord"),
        "landmark": (t.get("pickupLandmark") if t["tripDirection"] == "IN"
                     else t.get("dropLandmark")),
        "assigned": bool(t.get("cabAssigned")), "cab": t.get("cabReg"),
    }


def web_cab(c):
    """The trimmed shape netlify/functions/pool.mjs emits."""
    nd = c.get("vehicleNextTripDetails")
    return {
        "cabRegNo": c.get("cabRegNo"), "capacity": c.get("capacity"),
        "cabActive": c.get("cabActive"), "virtual": c.get("virtual"),
        "busyVehicle": c.get("busyVehicle"),
        "complianceStatus": c.get("complianceStatus"),
        "emptyLegInMetres": c.get("emptyLegInMetres"),
        "subVendorName": c.get("subVendorName"),
        "driver": (c.get("drivers") or [{}])[0].get("driverName") if c.get("drivers") else None,
        "nextTrip": ({"hour": nd.get("hour"), "min": nd.get("min"), "buid": nd.get("buid")}
                     if nd else None),
    }


# Must match GEO_DP in web/scripts/build-history.mjs. Both sides are rounded to
# it before scoring, so the comparison is made on exactly the values that ship
# rather than on a precision only the Python side ever sees. At 5 dp the two
# scorers disagreed by ~1e-4 on the kernel — not a porting bug, just different
# inputs, which is precisely the kind of false alarm that makes a check useless.
GEO_DP = 6


class RoundedHistory:
    """A History whose coordinates have been snapped to the shard's precision."""

    def __init__(self, hist):
        rnd = lambda r: (round(r[0], GEO_DP), round(r[1], GEO_DP)) + tuple(r[2:])
        self.rows = [rnd(r) for r in hist.rows]
        self.by_cab = {c: [rnd(r) for r in v] for c, v in hist.by_cab.items()}
        self.profiles = hist.profiles


def bundle_for(hist, office, direction):
    """Re-express a History as the shard format scorer.js consumes, so the JS
    side scores the EXACT rows the Python side did. This isolates the scorer:
    any difference is the porting, not the extract."""
    cabs, idx = [], {}
    rows = []
    # 8-tuple since the time-of-day work: (lat, lng, shift, vendor, cab, age,
    # fault, shift_minutes). The JS side re-derives minutes from the shift
    # string in its History constructor, so the shard shape stays 5 columns.
    for lat, lng, shift, _ven, cab, age, _f, _shm in hist.rows:
        i = idx.get(cab)
        if i is None:
            i = len(cabs); idx[cab] = i; cabs.append(cab)
        rows.append([i, round(lat, GEO_DP), round(lng, GEO_DP), shift, age])
    faults = {}
    for cab, p in hist.profiles.items():
        i = idx.get(cab)
        if i is None:                      # profile for a cab with no rows here
            i = len(cabs); idx[cab] = i; cabs.append(cab)
        faults.setdefault(office, {})[str(i)] = [p["n_trips"], round(p["fault"], 4)]
    return {"cabs": cabs, "shards": {"%s|%s" % (office, direction): rows},
            "faults": faults, "built_at": "", "from": "", "to": ""}


def main():
    m = MDS()
    m.login()
    guids = m.vendor_guids(BUID)
    day = date.today()
    day_ms = int(datetime.combine(day, datetime.min.time()).timestamp() * 1000)
    trips = m.trips(BUID, guids, day_ms)
    sys.stderr.write("%d trips for %s on %s\n" % (len(trips), BUID, day))
    if not trips:
        sys.exit("no trips — pick another BU or date")

    # wave state, exactly as service.py builds it
    assigned, chains = {}, {}
    for t in trips:
        cab = t.get("cabReg")
        if not cab:
            continue
        assigned.setdefault((t["shiftTime"], t["tripDirection"]), set()).add(cab)
        slat, slng = geo(t.get("tripStartGeoCord"))
        elat, elng = geo(t.get("tripEndGeoCord"))
        chains.setdefault(cab, []).append({
            "start": t["tripStartTime"] / 1000.0, "end": t["tripEndTime"] / 1000.0,
            "slat": slat, "slng": slng, "elat": elat, "elng": elng,
            "label": "%s %s" % (t["tripDirection"],
                                datetime.fromtimestamp(t["shiftTime"] / 1000).strftime("%H:%M")),
            "tripId": t["tripId"]})
    for c in chains:
        chains[c].sort(key=lambda a: a["start"])

    # Force-include trips MDS has no anchor coordinate for. They are ~0.7% of
    # live trips, they used to crash score_pool outright, and a random sample of
    # twelve would usually miss them entirely — so pin them deliberately.
    picked, seen_keys = [], set()
    for t in trips:
        d = "LOGIN" if t["tripDirection"] == "IN" else "LOGOUT"
        anchor = t.get("tripStartGeoCord") if d == "LOGIN" else t.get("tripEndGeoCord")
        if not anchor:
            picked.append(t)
            if len(picked) >= 2:
                break

    # Then spread the rest across offices, directions and both assigned states —
    # a fixture of twelve near-identical morning logins would prove very little.
    pinned = {t["tripId"] for t in picked}
    for want_assigned in (False, True):
        for t in trips:
            if bool(t.get("cabAssigned")) != want_assigned or t["tripId"] in pinned:
                continue
            k = (t["officeName"], t["tripDirection"], want_assigned)
            if k in seen_keys and len(picked) >= 4:
                continue
            seen_keys.add(k)
            picked.append(t)
            if len(picked) >= N:
                break
        if len(picked) >= N:
            break

    cases = []
    for t in picked:
        for cross in (False, True):
            direction = "LOGIN" if t["tripDirection"] == "IN" else "LOGOUT"
            hist = RoundedHistory(History.get(BUID, t["officeName"], direction))

            if cross:
                # one proxy trip per vendor, same office preferred — pickProxies()
                proxies = {}
                for x in trips:
                    v = x.get("vendorName")
                    cur = proxies.get(v)
                    if cur is None or (x.get("officeName") == t["officeName"]
                                       and cur.get("officeName") != t["officeName"]):
                        proxies[v] = x
                srcs = list(proxies.values())
            else:
                srcs = [t]

            pool = []
            for s in srcs:
                try:
                    pool.extend(m.vehicles(BUID, s["tripGuid"]))
                except Exception as e:
                    sys.stderr.write("  pool failed for %s: %s\n" % (s["tripId"], e))
            if not pool:
                continue

            already = set(assigned.get((t["shiftTime"], t["tripDirection"]), set()))
            ch = chains
            own = t.get("cabReg") if t.get("cabAssigned") else None
            if own:
                already.discard(own)
                ch = {c: [a for a in v if not (c == own and a["tripId"] == t["tripId"])]
                      for c, v in chains.items()}

            res = score_pool(t, pool, hist, wave_assigned=already, cross_vendor=cross,
                             chains=ch, explain_rejects=True, find_cab=own)
            top, n_elig, rejects = res[0], res[1], res[2]
            found = res[3] if own else None

            cases.append({
                "label": "%s %s %s %s layer%d" % (
                    t["tripId"], t["tripDirection"],
                    datetime.fromtimestamp(t["shiftTime"] / 1000).strftime("%H:%M"),
                    t["officeName"], 2 if cross else 1),
                "trip": web_trip(t),
                "pool": [web_cab(c) for c in pool],
                "bundle": bundle_for(hist, t["officeName"], direction),
                "office": t["officeName"], "direction": direction,
                "cross": cross,
                "waveAssigned": sorted(already),
                "chains": {k: v for k, v in ch.items()},
                "findCab": own,
                "expect": {
                    "eligible": n_elig,
                    "top": [{"cab": r["cab"], "tier": r["tier"], "no_anchor": r["no_anchor"],
                             "no_capacity": r["no_capacity"],
                             "exact_score": r["exact_score"], "kernel": r["kernel"],
                             "n_exact": r["n_exact"], "n_history": r["n_history"],
                             "nearest_km": r["nearest_km"], "fault_rate": r["fault_rate"],
                             "feasibility": r["feasibility"], "deadhead_km": r["deadhead_km"],
                             "evidence": r["evidence"], "confidence": r["confidence"]}
                            for r in top],
                    "rejects": sorted(rejects, key=lambda x: x["cab"]),
                    "found_rank": (found or {}).get("rank"),
                },
            })
            sys.stderr.write("  case %-52s %d cabs -> %d eligible\n"
                             % (cases[-1]["label"], len(pool), n_elig))

    json.dump({"buid": BUID, "date": day.isoformat(),
               "generated": datetime.now().isoformat(timespec="seconds"),
               "cases": cases}, sys.stdout)


if __name__ == "__main__":
    main()
