#!/usr/bin/env python3
"""
Recommendation service.

    python3 service.py [port]          default 8770

    GET /health
    GET /wave?buid=…&date=YYYY-MM-DD           trip list + vendor shortfall
    GET /recommend?buid=…&tripId=…[&layer=2][&debug=1]

WAVE STATE
----------
The deployer works in the MDS tab, not ours, so assignments have to be observed
rather than received. Every refresh rebuilds state FROM SCRATCH out of
trip/filter — never accumulated — so that un-assigning a cab releases it again.
A monotonic "already used" set would keep a released cab hidden for the session.

Two things come out of that same poll:
  * wave_assigned — cabs already holding a trip in this (shift, direction).
    Validated at 99.87%: a cab does not take two trips in one wave.
  * chains — every assigned trip today per cab, WITH geocodes, so feasibility
    can check "can it physically get from its last drop to this pickup, and on
    to its next pickup" rather than just comparing clock times.
"""
import base64, hmac, json, os, sys, threading, time, traceback
from datetime import datetime, date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import shadow
from mds import MDS, load_env
from recommend import History, score_pool, vendor_shortfall

WAVE_TTL_S = 30          # how stale the assignment picture may get
POOL_TTL_S = 120         # candidate pools change slower than assignments
PREFETCH_WORKERS = 5     # parallel pool warmers; MDS rate limits are unknown

_env = load_env()
UI_PASSWORD = _env.get("UI_PASSWORD", "").strip()   # empty = auth OFF (dev only)

_lock = threading.Lock()
_mds = MDS(_env)
_waves = {}              # (buid, date) -> {"t": epoch, "data": {...}}
_pools = {}              # cache key -> {"t": epoch, "data": [cab, ...]}
_auth_error = None       # surfaced to the UI instead of a blank screen


def _prune_caches():
    """Both caches key on (buid, guid/date) and entries are re-fetched after
    their TTL, but the DEAD keys — yesterday's waves, GUIDs of trips that no
    longer exist — were never removed, so a service left running for weeks
    accumulates them forever. Called under _lock."""
    now = time.time()
    for cache, ttl in ((_waves, WAVE_TTL_S), (_pools, POOL_TTL_S)):
        for k in [k for k, v in cache.items() if now - v["t"] > 20 * ttl]:
            del cache[k]


def _pool(buid, trip_guid):
    """Per-TRIP pool, cached. Cannot be shared between trips: emptyLegInMetres
    is computed relative to whichever trip was queried."""
    k = (buid, trip_guid)
    with _lock:
        hit = _pools.get(k)
        if hit and time.time() - hit["t"] < POOL_TTL_S:
            return hit["data"]
    d = _mds.vehicles(buid, trip_guid)
    with _lock:
        _pools[k] = {"t": time.time(), "data": d}
    return d


def prefetch(buid, trips):
    """Warm the pool cache for unassigned trips so clicking one is instant.
    Without this every click costs ~1.2 s waiting on MDS."""
    todo = [t["tripGuid"] for t in trips if not t.get("cabAssigned")]
    todo = [g for g in todo
            if not (_pools.get((buid, g)) and time.time() - _pools[(buid, g)]["t"] < POOL_TTL_S)]
    if not todo:
        return
    def work(q):
        while True:
            try:
                g = q.pop()
            except IndexError:
                return
            try:
                _pool(buid, g)
            except Exception:
                pass
    ts = [threading.Thread(target=work, args=(todo,), daemon=True)
          for _ in range(min(PREFETCH_WORKERS, len(todo)))]
    [t.start() for t in ts]


def _geo(s):
    try:
        a, b = str(s).split(",")
        return float(a), float(b)
    except Exception:
        return None, None


def wave(buid, day_iso, force=False):
    """Trips + assignment state for one BU-day. Cached WAVE_TTL_S."""
    key = (buid, day_iso)
    with _lock:
        _prune_caches()
        hit = _waves.get(key)
        if hit and not force and time.time() - hit["t"] < WAVE_TTL_S:
            return hit["data"]

    global _auth_error
    d = datetime.strptime(day_iso, "%Y-%m-%d").date()
    day_ms = int(datetime.combine(d, datetime.min.time()).timestamp() * 1000)
    try:
        guids = _mds.vendor_guids(buid)
        trips = _mds.trips(buid, guids, day_ms)
        _auth_error = None
    except Exception as e:
        # the realistic failure: the MDS password was rotated. Say so plainly
        # instead of letting the UI show an empty screen.
        if "login failed" in str(e).lower():
            _auth_error = ("MDS login failed — the password in .env is probably "
                           "out of date. Update MDS_PASSWORD and restart.")
        raise

    by_id, assigned, chains = {}, {}, {}
    for t in trips:
        by_id[str(t["tripId"])] = t
        cab = t.get("cabReg")
        if not cab:
            continue
        # one-cab-per-wave, keyed on (shift, direction)
        assigned.setdefault((t["shiftTime"], t["tripDirection"]), set()).add(cab)
        # chain, with real geocodes, for spatial feasibility
        slat, slng = _geo(t.get("tripStartGeoCord"))
        elat, elng = _geo(t.get("tripEndGeoCord"))
        chains.setdefault(cab, []).append({
            "start": t["tripStartTime"] / 1000.0, "end": t["tripEndTime"] / 1000.0,
            "slat": slat, "slng": slng, "elat": elat, "elng": elng,
            "label": "%s %s" % (t["tripDirection"],
                                datetime.fromtimestamp(t["shiftTime"] / 1000).strftime("%H:%M")),
        })
    for c in chains:
        chains[c].sort(key=lambda a: a["start"])

    data = {"buid": buid, "date": day_iso, "trips": trips, "by_id": by_id,
            "assigned": assigned, "chains": chains, "guids": guids,
            "fetched_at": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        _waves[key] = {"t": time.time(), "data": data}
    return data


def vendor_pools(w, trip=None, cross=False):
    """Layer 1 -> the trip's own pool (MDS scopes it to the trip's vendor).
    Layer 2 -> one pool per distinct vendor in the wave, unioned. There is no
    vendor parameter on the endpoint, so another vendor's cabs are only visible
    via one of that vendor's own trips.

    The proxy trip is chosen from the SAME OFFICE as the target when possible:
    emptyLegInMetres is computed against the proxy, so a proxy at another office
    (Ivy has IN-PUNE!) would make every deadhead — and the coarse cross-vendor
    deadhead gate — nonsense."""
    if not cross:
        return {trip.get("vendorName"): _pool(w["buid"], trip["tripGuid"])}
    want_office = trip.get("officeName") if trip else None
    proxies = {}
    for t in w["trips"]:
        v = t.get("vendorName")
        cur = proxies.get(v)
        if cur is None or (want_office
                           and t.get("officeName") == want_office
                           and cur.get("officeName") != want_office):
            proxies[v] = t
    return {v: _pool(w["buid"], p["tripGuid"]) for v, p in proxies.items()}


NO_VENDOR = ("", "none", "not assigned", "na", "null")


def has_vendor(trip):
    v = str(trip.get("vendorName") or "").strip().lower()
    return bool(v) and v not in NO_VENDOR


def shift_summary(trips):
    """One entry per WAVE — (direction, shift) — not per shift time. MDS lists
    'IN 19:30' and 'OUT 19:30' separately because they are different waves, and
    collapsing them hides half the picture. Includes fully-deployed waves so the
    shift list matches what the deployer sees in MDS."""
    agg = {}
    for t in trips:
        k = (t["tripDirection"],
             datetime.fromtimestamp(t["shiftTime"] / 1000).strftime("%H:%M"),
             t["officeName"])
        a = agg.setdefault(k, {"total": 0, "open": 0})
        a["total"] += 1
        if not t.get("cabAssigned"):
            a["open"] += 1
    return sorted(
        [{"direction": d, "shift": s, "office": o, "total": v["total"], "open": v["open"]}
         for (d, s, o), v in agg.items()],
        key=lambda r: (r["shift"], r["direction"], r["office"]))


def recommend(buid, trip_id, cross=None, debug=False, evaluate=False, day_iso=None):
    """cross=None -> decide automatically. With no vendor on the trip there is no
    'same vendor' to stay within, so Layer 2 is the only sensible default.

    day_iso: the wave date. Deployers work the NEXT day's waves at midnight —
    hardcoding today() here made every trip the UI showed for tomorrow
    unresolvable ("trip not in today's wave").

    evaluate=True is an OFF-BY-DEFAULT testing mode for already-deployed trips:
    it un-hides the cab currently on the trip (MDS marks it busy *because* of
    this trip) so we can report where it ranked. It must never be on in normal
    use — it would let an already-deployed cab be recommended again."""
    w = wave(buid, day_iso or date.today().isoformat())
    trip = w["by_id"].get(str(trip_id))
    if not trip:
        return {"error": "trip %s not in today's wave for %s" % (trip_id, buid)}

    auto = cross is None
    if auto:
        cross = not has_vendor(trip)

    direction = "LOGIN" if trip["tripDirection"] == "IN" else "LOGOUT"
    hist = History.get(buid, trip["officeName"], direction)
    pools = vendor_pools(w, trip, cross)
    pool = [c for v in pools.values() for c in v]
    already = set(w["assigned"].get((trip["shiftTime"], trip["tripDirection"]), set()))
    chains = w["chains"]
    own = trip.get("cabReg") if evaluate else None
    if own:
        # evaluation only: don't exclude the cab on THIS trip, and drop this trip
        # from its own chain, so it can be scored like any other candidate
        already.discard(own)
        chains = {c: [a for a in v
                      if not (c == own and abs(a["start"] - trip["tripStartTime"] / 1000) < 60)]
                  for c, v in chains.items()}

    res = score_pool(trip, pool, hist, wave_assigned=already, cross_vendor=cross,
                     chains=chains, explain_rejects=debug, find_cab=own)
    top, n_elig = res[0], res[1]
    out = {
        "trip": {"id": trip["tripId"], "direction": trip["tripDirection"],
                 "shift": datetime.fromtimestamp(trip["shiftTime"] / 1000).strftime("%H:%M"),
                 "office": trip["officeName"], "landmark": trip.get("pickupLandmark"),
                 "capacity": trip.get("plannedCabCapacity"), "vendor": trip.get("vendorName"),
                 "cut_off": datetime.fromtimestamp(
                     trip["assignmentCutOffTime"] / 1000).strftime("%H:%M")},
        "layer": 2 if cross else 1,
        "layer_auto": auto,
        "vendor_assigned": has_vendor(trip),
        "pool_size": len(pool), "eligible": n_elig,
        "excluded_already_deployed": len(already),
        "history_trips": len(hist.rows),
        "recommendations": top,
        "wave_fetched_at": w["fetched_at"],
    }
    if debug:
        out["rejects"] = res[2][:40]
    if own:                                        # trip already deployed
        out["actual"] = res[-1]                    # where the real cab ranked
    # Shadow-log GENUINE PREDICTIONS ONLY: the trip must still be open and this
    # must not be an evaluation replay. Logging assigned/evaluate calls would
    # contaminate the acceptance metric with post-hoc lookups made against an
    # already-drained pool — the exact survivorship bias evaluate.py warns about.
    if not trip.get("cabAssigned") and not evaluate:
        shadow.log(buid, trip, out["layer"], out, trip_day=w["date"])   # never raises
    return out


# ────────────────────────────── SWEEPER ──────────────────────────────
# Measurement across ALL sites and shifts, with no one clicking anything:
# every SWEEP_INTERVAL_MIN, generate + shadow-log recommendations for every
# still-open trip at every BU in the history DB. Deployers work normally in
# MDS; the nightly reconcile fills in what they chose; `shadow.py week`
# compares. Predictions logged after a trip was assigned are discarded by the
# report (ts vs cab_allocation_time), so a slow sweep can't fake accuracy.

SWEEP_INTERVAL_MIN = int(_env.get("SWEEP_INTERVAL_MIN", "30") or 0)
SWEEP_RELOG_MIN = 60      # re-log an open trip at most this often — later logs
                          # are more accurate (wave exclusions grow), and the
                          # report keeps only the last one before assignment


def _sweep_once(buids=None):
    import sqlite3
    dbc = sqlite3.connect(os.path.join(ROOT, "data", "history.db"), timeout=15)
    if buids is None:
        buids = [r[0] for r in dbc.execute("SELECT DISTINCT bunit_id FROM trips")]
    cut = (datetime.now() - timedelta(minutes=SWEEP_RELOG_MIN)).isoformat(timespec="seconds")
    recent = {(b, t) for b, t in
              dbc.execute("SELECT buid, trip_id FROM reco_log WHERE ts > ?", (cut,))}
    dbc.close()

    days = [date.today().isoformat()]
    if datetime.now().hour >= 18:      # evening onwards: tomorrow's waves exist
        days.append((date.today() + timedelta(days=1)).isoformat())

    logged = errs = 0
    for buid in buids:
        for day_iso in days:
            try:
                w = wave(buid, day_iso)
            except Exception:
                errs += 1
                continue
            for t in w["trips"]:
                if t.get("cabAssigned") or (buid, str(t["tripId"])) in recent:
                    continue
                try:
                    recommend(buid, t["tripId"], day_iso=day_iso)   # logs itself
                    logged += 1
                except Exception:
                    errs += 1
                time.sleep(0.15)       # be gentle with MDS
            time.sleep(0.5)
    sys.stderr.write("%s  sweep: %d BUs, %d predictions logged, %d errors\n"
                     % (datetime.now().strftime("%H:%M:%S"), len(buids), logged, errs))
    return logged


def _sweeper():
    time.sleep(90)                     # let startup + first UI loads settle
    while True:
        try:
            _sweep_once()
        except Exception:
            traceback.print_exc()
        time.sleep(SWEEP_INTERVAL_MIN * 60)


class Handler(BaseHTTPRequestHandler):
    def _authorized(self):
        """HTTP Basic auth against UI_PASSWORD (any username). Browser shows its
        native prompt on first load and re-sends credentials on every same-origin
        request afterwards — zero client-side code. Empty UI_PASSWORD disables
        auth for local development; bootstrap.sh generates one on servers."""
        if not UI_PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(h[6:]).decode("utf-8", "replace").split(":", 1)[-1]
        except Exception:
            return False
        return hmac.compare_digest(supplied, UI_PASSWORD)

    def _deny(self):
        b = b'{"error": "authentication required"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Cab Recommender"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send(self, obj, code=200):
        b = json.dumps(obj, indent=1, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send_html(self, path):
        try:
            b = open(path, "rb").read()
        except OSError:
            return self._send({"error": "ui not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/health":            # the only unauthenticated route
                return self._send({"ok": True, "waves_cached": len(_waves)})
            if not self._authorized():
                return self._deny()
            if u.path in ("/", "/index.html"):
                return self._send_html(os.path.join(ROOT, "static", "index.html"))
            if u.path == "/buids":
                # Every BU in the synced history. history_extract.sql is already
                # Hyderabad-scoped, so this IS the deployable set — no hardcoding.
                import sqlite3
                c = sqlite3.connect(os.path.join(ROOT, "data", "history.db"))
                rows = c.execute("""SELECT bunit_id, COUNT(*) n,
                                           COUNT(DISTINCT office) offices
                                    FROM trips GROUP BY bunit_id ORDER BY n DESC""").fetchall()
                c.close()
                return self._send([{"buid": b, "trips_30d": n, "offices": o}
                                   for b, n, o in rows])
            buid = q.get("buid", "ivycomptech-IVYHyd")
            if u.path == "/wave":
                w = wave(buid, q.get("date", date.today().isoformat()),
                         force=q.get("force") == "1")
                un = [t for t in w["trips"] if not t.get("cabAssigned")]
                prefetch(buid, w["trips"])          # warm pools so clicks are instant
                return self._send({
                    "buid": buid, "date": w["date"], "fetched_at": w["fetched_at"],
                    "auth_error": _auth_error,
                    "trips": len(w["trips"]), "unassigned": len(un),
                    "cabs_deployed": sum(len(v) for v in w["assigned"].values()),
                    "shifts": shift_summary(w["trips"]),
                    # ALL trips, with an assigned flag — the client filters.
                    # Sending only unassigned made the "all trips" filter a no-op
                    # and hid fully-deployed shifts from the shift list.
                    "all_trips": [
                        {"tripId": t["tripId"], "direction": t["tripDirection"],
                         "shift": datetime.fromtimestamp(t["shiftTime"] / 1000).strftime("%H:%M"),
                         "office": t["officeName"],
                         # for a logout the meaningful end is the DROP, not the pickup
                         "landmark": (t.get("pickupLandmark") if t["tripDirection"] == "IN"
                                      else t.get("dropLandmark")),
                         "vendor": t.get("vendorName"), "capacity": t.get("plannedCabCapacity"),
                         "cut_off": datetime.fromtimestamp(
                             t["assignmentCutOffTime"] / 1000).strftime("%H:%M"),
                         "assigned": bool(t.get("cabAssigned")), "cab": t.get("cabReg"),
                         "has_vendor": has_vendor(t)}
                        for t in w["trips"]],
                })
            if u.path == "/recommend":
                if not q.get("tripId"):
                    return self._send({"error": "tripId required"}, 400)
                # no ?layer= -> let the server pick (Layer 2 when no vendor)
                lay = q.get("layer")
                return self._send(recommend(buid, q["tripId"],
                                            cross=(None if lay is None else lay == "2"),
                                            debug=q.get("debug") == "1",
                                            evaluate=q.get("evaluate") == "1",
                                            day_iso=q.get("date")))
            self._send({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._send({"error": str(e)}, 500)

    def log_message(self, fmt, *a):
        sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % a))


class Server(ThreadingHTTPServer):
    allow_reuse_address = True      # otherwise a restart hits TIME_WAIT for ~60s
    daemon_threads = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno in (48, 98):     # EADDRINUSE
            sys.exit("Port %d is already in use — the service is probably already "
                     "running.\n  Open  http://127.0.0.1:%d\n"
                     "  Or stop it:  lsof -ti :%d | xargs kill\n"
                     "  Or use another port:  python3 service.py 8771" % (port, port, port))
        raise
    try:
        _mds.login()
    except Exception as e:
        sys.exit("MDS login failed: %s\nCheck MDS_USERNAME / MDS_PASSWORD in .env" % e)
    if UI_PASSWORD:
        print("UI auth ON (any username + the UI_PASSWORD from .env)")
    else:
        print("!! UI auth OFF — set UI_PASSWORD in .env before exposing this "
              "beyond localhost")
    if SWEEP_INTERVAL_MIN > 0:
        threading.Thread(target=_sweeper, daemon=True).start()
        print("sweeper ON: predicting every open trip at every BU each %d min "
              "(SWEEP_INTERVAL_MIN=0 disables)" % SWEEP_INTERVAL_MIN)
    print("MDS auth ok — serving on http://127.0.0.1:%d  (ctrl-C to stop)" % port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
