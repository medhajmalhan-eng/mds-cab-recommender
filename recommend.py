#!/usr/bin/env python3
"""
Scoring engine: local 30-day history + live MDS candidate pool -> ranked cabs.

Backtested on 41,203 trips across 6 Hyderabad BU-offices (rolling 30d, no
leakage): 26.6% top-1 / 45.5% top-3 / 55.2% top-5 against what the deployer
actually did.

  hard filters -> exact-route tier -> fallback kernel -> time-of-day -> tiebreak

The time-of-day terms were added 2026-08-08 and are worth, re-measured on the
same footing (experiment.py, baseline vs tuned on identical trips):
    17,174 trips  +2.2 top-1  +4.6 top-3  +5.4 top-5
     6,708 trips  +2.9        +5.5        +5.5      <- held out, tuning never
                                                       saw this period
Rare shifts, which is what they were built for: 41.5 -> 52.1 top-5.

Things that were tested and made it WORSE, so don't re-add them:
  - employee<->cab affinity (<1pt)
  - windows past 30 days (top-1 flat; only +1pt top-5)
  - greedy global assignment (20.4% vs 24.2% — cabs legitimately do 3-5 trips/day)
  - reliability as a ranking term rather than a tiebreak (49.3% -> 49.1%)
  - the garage anchor applied globally (helps LOGIN-first only)
  - opportunity-cost re-ranking, i.e. demoting a cab that another trip in the
    same wave needs more (-2.0 top-1, and no gain in wave-level set overlap)
  - wave-level assignment INSIDE the ranking: it trades -3.8 top-1 for +11.3
    wave-set. That is a real effect and might justify a separate "plan the whole
    wave" view, but the deployer clicks one trip and reads five cabs, so it must
    not replace the per-trip list.
"""
import math, os, sqlite3, time
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "data", "history.db")

# ── scoring constants (all measured, do not tune casually) ──
KERNEL_KM      = 3.0    # exp(-d/KERNEL_KM)
KERNEL_CAP_KM  = 10.0   # beyond this, no credit. 3km was too tight: cost 2.7pts top-5
EXACT_KM       = 1.0    # "same route" radius for the dominant tier
SHIFT_BONUS    = 2.0    # multiplier is (1 + SHIFT_BONUS * shift_similarity)
RECENCY_HALFLIFE_D = 21.0
SPECIFICITY    = 0.5    # divide by n_cab**this. Rewards cabs that ONLY do this area

# ── time-of-day terms (added 2026-08-08) ────────────────────────────────
# Both were measured with experiment.py. Held out on 6,708 trips from a period
# the tuning never saw: top-1 20.8 -> 23.7, top-5 47.0 -> 52.6. Confirmed on
# 17,174 trips overall (+2.2 / +5.4). Neither works alone — separately they are
# +0.2 and +0.5, which is noise; together they are worth ~5pts of top-5.
#
# Why they were needed: shift matching used to be exact STRING equality, so a
# shift with little history had the x3 bonus — the strongest term in the model —
# permanently switched off. A real 02:30 wave at Ivy had 8 historical trips at
# that time out of 12,498 at the site: nothing scored `strong`, every card read
# "no exact match", and ranking collapsed onto daytime area familiarity while
# the deployer picked night regulars. Rare shifts go 41.5 -> 52.1 top-5.
TOD_TAU        = 30.0   # minutes. exp(-|dt|/TOD_TAU) replaces the 0/1 shift match.
                        # Swept 15..180; 30-45 flat, degrades past 90.
TIER_DELTA_MIN = 0      # tier 1 still requires an EXACT time match. Loosening it
                        # was not tested — do not change without re-running.
DUTY_W         = 16.0   # weight on "does this cab work this hour at all", scored
                        # with NO distance restriction. Swept 1..32: top-1 plateaus
                        # near 16, top-5 still climbing at 32. 16 is the knee.
DUTY_TAU       = 60.0   # minutes, for the duty term. 60 beat 120 and 240.
# SOFT TIER — TESTED AND REJECTED (2026-08-08). Replacing the lexicographic
# exact-route tier with a multiplicative blend looked good on the honest
# backtest (+2.1 top-1 in-sample, +1.5 held out, ~9k trips) but FAILED the
# paired test on 146 live pre-assignment decisions: 1 gain vs 4 losses in
# top-5. The backtest population (all waves, daytime-dense) is not the live
# sample; the hard tier's "one exact trip beats any area cab" is apparently
# closer to how deployers actually think than the blend. Do not re-add without
# a live paired win.
FEASIBILITY_BUFFER_MIN = 30
MAX_DEADHEAD_KM = 60    # a cab this far from the pickup cannot serve it. Vendor
                        # pools are city-wide — Ivy has a Pune office, so a
                        # Hyderabad trip's pool contains MH-plated Pune cabs
                        # ~490 km away. The chain check only catches those that
                        # happen to have another trip that day; an idle one has
                        # no chain and would sail through.
MAX_DEADHEAD_XV_KM = 120  # coarse version of the same gate for CROSS-VENDOR pools.
                          # There the deadhead was computed against a proxy trip of
                          # that vendor (same office preferred), so the number is
                          # approximate — but out-of-city cabs read ~480 km, so a
                          # loose gate still removes them without false rejects.

KM_PER_DEG_LAT = 111.0
KM_PER_DEG_LNG = 105.9   # at Hyderabad's latitude (~17.4 N)


def _km(alat, alng, blat, blng):
    return math.hypot((alat - blat) * KM_PER_DEG_LAT, (alng - blng) * KM_PER_DEG_LNG)


def _mins(hhmm):
    """'02:30' -> 150. None when unparseable — the extract carries the literal
    string 'null' for a missing shift, and treating that as a time would match
    it against everything."""
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _circ(a, b):
    """Minutes between two times of day, the short way round the clock. 23:45 and
    00:15 are 30 minutes apart, not 1410 — and night waves, which is where this
    whole term earns its keep, sit right on that boundary."""
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


class History:
    """30-day history for one (buid, office, direction), held in memory."""

    _cache = {}

    def __init__(self, buid, office, direction):
        self.rows = []          # (lat, lng, shift, eff_vendor, cab, age_days, driver_fault)
        self.by_cab = {}
        self.n_by_cab = {}
        db = sqlite3.connect(DB)
        today = datetime.now().date()
        q = """SELECT anchor_lat, anchor_lng, shift, eff_vendor, cab_reg, day, driver_fault
               FROM trips WHERE bunit_id=? AND office=? AND trip_direction=?"""
        for lat, lng, shift, ven, cab, day, fault in db.execute(q, (buid, office, direction)):
            if lat is None or not cab:
                continue
            age = (today - datetime.strptime(day, "%Y-%m-%d").date()).days
            # shift minutes precomputed: the scorer needs it for every row on
            # every trip, and re-parsing 12k strings per click is wasted work
            rec = (lat, lng, shift, ven, cab, age, fault or 0, _mins(shift))
            self.rows.append(rec)
            self.by_cab.setdefault(cab, []).append(rec)
        self.n_by_cab = {c: len(v) for c, v in self.by_cab.items()}
        self.profiles = {}
        for cab, n, fr in db.execute(
                "SELECT cab_reg, n_trips, driver_fault_rate FROM cab_profiles "
                "WHERE bunit_id=? AND office=?", (buid, office)):
            self.profiles[cab] = {"n_trips": n, "fault": fr or 0.0}
        db.close()

    @classmethod
    def get(cls, buid, office, direction, ttl=900):
        k = (buid, office, direction)
        hit = cls._cache.get(k)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        h = cls(buid, office, direction)
        cls._cache[k] = (time.time(), h)
        return h


def _hhmm(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


ROAD_FACTOR = 1.4      # straight-line km -> road km, Hyderabad
AVG_KMH     = 20.0     # MEASURED, not guessed: median planned_km / planned
                       # duration across 24,903 real Ivy trips = 20.2 km/h.
                       # This was 25 and that made the feasibility check ~24%
                       # optimistic — it accepted chains a cab could not make.
                       # p25 is 17 km/h, so 20 is central, not conservative.


def travel_min(km):
    return (km * ROAD_FACTOR) / AVG_KMH * 60.0


def feasible(cab, trip, chain=None, is_own=False):
    """Can this cab actually take this trip, given what it's already committed to?

    Two levels, because MDS gives two different qualities of information:

    FULL (spatial) — when `chain` has the cab's other assignments for the day,
    each with real geocodes from trip/filter. Then we check both legs:
        prev trip END  -> this trip START   must fit in the gap
        this trip END  -> next trip START   must fit in the gap
    Travel time is straight-line x ROAD_FACTOR / AVG_KMH.

    TIME-ONLY (fallback) — vehicleLast/NextTripDetails carry {hour,min,direction,
    buid} with NO location and no date, so for commitments outside the BUs we can
    see, only the clock can be checked. Those are flagged so the caller knows the
    check was weaker, rather than silently passing.

    CAVEAT on the fallback: it is undocumented whether hour/min is that trip's
    start or end. Read as a start time — the conservative choice for a NEXT trip,
    which is the one that can actually be blocked."""
    # `is_own`: this cab is the one already deployed on THIS trip (evaluation
    # mode). MDS flags it busy *because* of this trip, and points its "next
    # trip" at this trip — so both signals must be ignored, or the very cab
    # we're trying to score is always rejected.
    if cab.get("busyVehicle") and not is_own:
        return False, "busy", "full"

    t_start = trip["tripStartTime"] / 1000.0
    t_end = trip["tripEndTime"] / 1000.0
    t_slat, t_slng = _parse_geo(trip.get("tripStartGeoCord"))
    t_elat, t_elng = _parse_geo(trip.get("tripEndGeoCord"))
    buf = FEASIBILITY_BUFFER_MIN * 60

    # ---------- FULL: real chain with geocodes ----------
    if chain:
        for a in chain:
            if a["end"] > t_start - buf and a["start"] < t_end + buf:
                return False, "overlaps its %s trip" % a.get("label", "other"), "full"
        prev = max((a for a in chain if a["end"] <= t_start), key=lambda a: a["end"], default=None)
        if prev and prev.get("elat") is not None and t_slat is not None:
            need = travel_min(_km(prev["elat"], prev["elng"], t_slat, t_slng)) * 60
            if prev["end"] + need > t_start:
                return False, "can't reach pickup from its %s trip (needs %.0f min, has %.0f)" % (
                    prev.get("label", "previous"), need / 60,
                    (t_start - prev["end"]) / 60), "full"
        nxt = min((a for a in chain if a["start"] >= t_end), key=lambda a: a["start"], default=None)
        if nxt and nxt.get("slat") is not None and t_elat is not None:
            need = travel_min(_km(t_elat, t_elng, nxt["slat"], nxt["slng"])) * 60
            if t_end + need > nxt["start"]:
                return False, "can't reach its next %s trip (needs %.0f min, has %.0f)" % (
                    nxt.get("label", ""), need / 60, (nxt["start"] - t_end) / 60), "full"

    # ---------- TIME-ONLY: commitments we can only see the clock for ----------
    nd = cab.get("vehicleNextTripDetails")
    if nd and nd.get("hour") is not None and not is_own:
        end_dt = datetime.fromtimestamp(t_end)
        start_dt = datetime.fromtimestamp(t_start)
        nd_min = nd["hour"] * 60 + nd.get("min", 0)
        # vehicleNextTripDetails has NO date. If its time-of-day is before this
        # trip even starts, it is almost certainly tomorrow's trip — comparing it
        # to today's clock would false-reject every cab whose next duty is an
        # early-morning login (exactly the cabs a midnight deployer is placing).
        if nd_min >= start_dt.hour * 60 + start_dt.minute:
            if nd_min < end_dt.hour * 60 + end_dt.minute + FEASIBILITY_BUFFER_MIN:
                return False, "next trip %02d:%02d (%s)" % (
                    nd["hour"], nd.get("min", 0), nd.get("buid", "?")), "time-only"
    return True, None, ("full" if chain else "time-only")


def _parse_geo(s):
    try:
        a, b = str(s).split(",")
        return float(a), float(b)
    except Exception:
        return None, None


def _r(x, dp):
    """Round half-UP, matching JavaScript.

    Python rounds half-to-EVEN everywhere — both round() and "%.1f" — while JS's
    Math.round and toFixed round half-up. round(2.25, 1) is 2.2 in Python and
    2.3 in JS. That looks like an edge case and is not: nearest_km is snapped to
    2 dp and then displayed at 1 dp, which manufactures exact .x5 values roughly
    one time in ten. Both ports must agree on every digit a deployer can see,
    and verify.mjs compares these strings."""
    m = 10 ** dp
    return math.floor(x * m + 0.5) / m


def _n(x):
    """Render a missing value as "?" rather than the language's own spelling.
    These strings are shown to deployers AND compared between this module and
    web/public/scorer.js — "None" vs "null" would be a permanent false failure
    in the verification harness."""
    return "?" if x is None else str(x)


def score_pool(trip, pool, hist, wave_assigned=(), cross_vendor=False, topn=5,
               chains=None, explain_rejects=False, find_cab=None):
    """Rank a live MDS candidate pool against local history.

    `cross_vendor` only changes how deadhead is reported: MDS computes
    emptyLegInMetres for the trip that was queried, so when the pool came from a
    different trip (the only way to see another vendor's cabs) it is wrong and
    must be suppressed rather than shown."""
    direction = "LOGIN" if trip["tripDirection"] == "IN" else "LOGOUT"
    geo = trip["tripStartGeoCord"] if direction == "LOGIN" else trip["tripEndGeoCord"]
    # ~0.7% of live trips carry no anchor coordinate at all (13 of 1,973 measured
    # across three BUs, some of them unassigned). This used to be
    # `float(x) for x in geo.split(",")`, which raised AttributeError on None and
    # 500'd the whole request — so the trips a deployer most needs help with were
    # the ones that returned nothing.
    tlat, tlng = _parse_geo(geo)
    no_anchor = tlat is None
    # Some trips carry no plannedCabCapacity. Filtering on it then rejected the
    # ENTIRE pool — 190 of 190 cabs at goc-GocHyd — and the deployer got an empty
    # list with no reason given. With no capacity stated there is no constraint
    # to enforce, so enforce none and say so.
    want_cap = trip.get("plannedCabCapacity")
    no_cap = not want_cap
    tshift = _hhmm(trip["shiftTime"])
    tshift_m = _mins(tshift)
    hl = RECENCY_HALFLIFE_D

    out, rejects = [], []
    seen = set()
    for c in pool:
        cab = c.get("cabRegNo")
        if not cab or cab in seen:          # Layer 2 unions pools; a cab can repeat
            continue
        seen.add(cab)

        def drop(reason):
            if explain_rejects:
                rejects.append({"cab": cab, "reason": reason})

        # the deployer already used this cab elsewhere in this wave
        if cab in wave_assigned:
            drop("already deployed in this wave"); continue
        if not c.get("cabActive") or c.get("virtual"):
            drop("inactive/virtual"); continue
        if c.get("complianceStatus") != "Compliant":
            drop("non-compliant"); continue
        # "?" rather than the language's own null spelling, so this string is
        # identical in recommend.py and scorer.js and verify.mjs can compare it.
        if not no_cap and c.get("capacity") != want_cap:
            drop("capacity %s != %s" % (_n(c.get("capacity")), _n(want_cap)))
            continue
        # Deadhead gate. Exact on the trip's own pool; coarse (wider limit) on a
        # cross-vendor pool, where MDS computed emptyLegInMetres against a proxy
        # trip of that vendor. Without the coarse gate an IDLE out-of-city cab —
        # no chain, so nothing else rejects it — could surface in Layer 2.
        el = c.get("emptyLegInMetres")
        lim = MAX_DEADHEAD_XV_KM if cross_vendor else MAX_DEADHEAD_KM
        if el is not None and el >= 0 and el / 1000.0 > lim:
            drop("%.0f km away (limit %d)" % (el / 1000.0, lim)); continue
        ok, why, depth = feasible(c, trip, (chains or {}).get(cab), is_own=(cab == find_cab))
        if not ok:
            drop(why); continue

        rows = hist.by_cab.get(cab, [])
        n_exact = exact_w = kern = duty = 0.0
        nearest = None
        # Without a coordinate there is no route to match on, so every history
        # term stays zero and the ranking falls through to deadhead. Saying that
        # plainly beats inventing a similarity score from nothing.
        for lat, lng, shift, ven, _c, age, fault, shm in ([] if no_anchor else rows):
            r = 0.5 ** (age / hl)
            # Duty accumulates BEFORE the distance gate, on purpose: "does this
            # cab work this hour" is a fact about the cab, not about this pickup.
            # Filtering it by distance would collapse it back into the kernel.
            if shm is not None and tshift_m is not None:
                duty += math.exp(-_circ(shm, tshift_m) / DUTY_TAU) * r
            d = _km(tlat, tlng, lat, lng)
            if nearest is None or d < nearest:
                nearest = d
            if d > KERNEL_CAP_KM:
                continue
            sim = (0.0 if (shm is None or tshift_m is None)
                   else math.exp(-_circ(shm, tshift_m) / TOD_TAU))
            kern += math.exp(-d / KERNEL_KM) * (1 + SHIFT_BONUS * sim) * r
            if (d <= EXACT_KM and shm is not None and tshift_m is not None
                    and _circ(shm, tshift_m) <= TIER_DELTA_MIN):
                n_exact += 1
                exact_w += r
        n = max(len(rows), 1)
        kern /= n ** SPECIFICITY
        kern *= (1 + DUTY_W * (duty / n))      # specificity first, then duty

        prof = hist.profiles.get(cab, {})
        el = c.get("emptyLegInMetres")
        out.append({
            "cab": cab,
            "subvendor": c.get("subVendorName"),
            "driver": (c.get("drivers") or [{}])[0].get("driverName")
                      if c.get("drivers") else None,
            "tier": 1 if n_exact > 0 else 2,
            "exact_score": exact_w,
            "kernel": kern,
            "n_exact": int(n_exact),
            "n_history": len(rows),
            "nearest_km": _r(nearest, 1) if nearest is not None else None,
            "fault_rate": round(prof.get("fault", 0.0), 4),
            # "full" = chain checked with real geocodes; "time-only" = clock only,
            # because that commitment sits in a BU we can't see trips for
            "feasibility": depth,
            # suppressed for cross-vendor: the number belongs to a different trip
            "deadhead_km": (_r(el / 1000, 1)
                            if (el is not None and el >= 0 and not cross_vendor) else None),
            "no_anchor": no_anchor,
            "no_capacity": no_cap,
        })

    if no_anchor:
        # Nothing to rank on but proximity. Cabs with no deadhead figure sort
        # last rather than first — an unknown distance is not a short one.
        out.sort(key=lambda r: (r["deadhead_km"] if r["deadhead_km"] is not None else 9e9,
                                r["fault_rate"]))
    else:
        out.sort(key=lambda r: (r["tier"], -r["exact_score"], -r["kernel"], r["fault_rate"]))
    for r in out:
        r["evidence"] = evidence(r)
        r["confidence"] = ("weak" if no_anchor else
                           ("strong" if r["tier"] == 1 else
                            ("medium" if r["n_history"] else "weak")))

    # For an already-deployed trip: where did the cab the deployer actually chose
    # land in our ranking? Lets a whole finished wave be checked at a glance,
    # without waiting for shadow-mode reconciliation.
    found = None
    if find_cab:
        for i, r in enumerate(out):
            if r["cab"] == find_cab:
                found = dict(r, rank=i + 1)
                break
        if found is None:
            in_pool = any(c.get("cabRegNo") == find_cab for c in pool)
            found = {"cab": find_cab, "rank": None, "in_pool": in_pool,
                     "why": next((x["reason"] for x in rejects if x["cab"] == find_cab),
                                 "filtered out — re-run with debug=1 for the reason"
                                 if in_pool else "not offered by MDS for this trip")}

    res = [out[:topn], len(out)]
    if explain_rejects:
        res.append(rejects)
    if find_cab:
        res.append(found)
    return tuple(res)


def evidence(r):
    if r.get("no_anchor"):
        s = "MDS has no pickup coordinate for this trip — cannot match on route"
        if r["deadhead_km"] is not None:
            s += " · nearest available, %.1f km away" % r["deadhead_km"]
        if r["fault_rate"] >= 0.10:
            # int(x + 0.5), not "%.0f": Python's % formatting rounds half-to-EVEN,
            # so 12.5 renders as "12" while JavaScript's Math.round gives 13.
            # fault_rate is a ratio of small integers (1/8 = 0.125 is common), so
            # that boundary is hit routinely and the two ports would disagree on
            # a number the deployer can see. Half-up in both.
            s += " · ⚠ driver-fault %d%%" % int(100 * r["fault_rate"] + 0.5)
        return s
    if r["n_exact"]:
        s = "did this route %d× in 30d (nearest %.1f km)" % (r["n_exact"], r["nearest_km"])
    elif r["n_history"]:
        s = "no exact match; %d trips in this area, nearest %.1f km" % (
            r["n_history"], r["nearest_km"])
    else:
        s = "no history at this site"
    if r["deadhead_km"] is not None:
        s += " · %.1f km away now" % r["deadhead_km"]
    if r["fault_rate"] >= 0.10:
        s += " · ⚠ driver-fault %d%%" % int(100 * r["fault_rate"] + 0.5)   # half-up; see above
    return s


def vendor_shortfall(trips, pools):
    """Vendor says "I've got 10 trips and only 5 vehicles". Detect it first.

    MUST be computed PER WAVE — (shift, direction) — not per day. A cab does
    3-5 trips across a day, so comparing a day's trips to the cab count invents
    a huge shortfall that isn't real. Within one wave it IS one cab per trip
    (validated at 99.87%), so there the comparison is meaningful.

    Counts only what's still open: unassigned trips vs cabs not yet used in
    that wave. A vendor with 10 trips and 5 cabs is fine once 5 are deployed
    and 5 remain — it's short by 0 at that point, not 5.

    pools: {vendorName: [cab, ...]} — one live pool per vendor."""
    usable = {}
    for ven, cabs in pools.items():
        usable[ven] = {c["cabRegNo"] for c in cabs
                       if c.get("cabRegNo") and c.get("cabActive") and not c.get("virtual")
                       and not c.get("busyVehicle")
                       and c.get("complianceStatus") == "Compliant"}

    waves = {}
    for t in trips:
        key = (t.get("shiftTime"), t.get("tripDirection"), t.get("vendorName"))
        w = waves.setdefault(key, {"open": 0, "taken": set()})
        if t.get("cabAssigned"):
            if t.get("cabReg"):
                w["taken"].add(t["cabReg"])
        else:
            w["open"] += 1

    out = []
    for (shift_ms, direction, ven), w in waves.items():
        if not w["open"] or ven not in usable:
            continue
        free = len(usable[ven] - w["taken"])
        if w["open"] > free:
            out.append({
                "vendor": ven, "direction": direction,
                "shift": datetime.fromtimestamp(shift_ms / 1000).strftime("%H:%M"),
                "trips_open": w["open"], "cabs_free": free,
                "short_by": w["open"] - free})
    return sorted(out, key=lambda r: -r["short_by"])
