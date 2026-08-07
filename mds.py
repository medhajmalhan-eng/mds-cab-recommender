#!/usr/bin/env python3
"""
MDS API client.

Everything here was verified against production on 2026-08-07. The two
non-obvious things, both of which cost real time to find:

  1. `Authorization: <raw token>` — no "Bearer" prefix. Chrome's HAR export
     STRIPS Authorization headers, so this header is invisible in .har files
     and only appears in "Copy as cURL". Without it every endpoint returns an
     empty-body 401 from Spring Security, which looks like a bad token.

  2. /fis/auth/login returns HTTP 200 even on failure, with the error in
     `successStatus`. Data endpoints DO use a proper 401. So auth failure has
     to be detected two different ways depending on the endpoint.
"""
import json, os, subprocess, sys, time, urllib.parse
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
TOKEN_CACHE = os.path.join(ROOT, "data", ".token_cache.json")
TOKEN_TTL_S = 4 * 3600          # measured exactly 4.0h (iat -> exp)
REFRESH_MARGIN_S = 20 * 60      # refresh early so live requests never eat a 401


KEYS = ("MDS_BASE_URL", "MDS_USERNAME", "MDS_PASSWORD", "UI_PASSWORD",
        "APPS_SCRIPT_EXEC_URL", "APPS_SCRIPT_TOKEN", "METABASE_CARD_ID",
        "METABASE_DB_ID", "HISTORY_WINDOW_DAYS", "SWEEP_INTERVAL_MIN")


def load_env():
    """Config from a .env file when present, else from real environment
    variables. Container platforms (Fly, Render, Docker) inject secrets as env
    vars and have no .env, so requiring the file made the app undeployable
    anywhere but a VM. Real env vars win, so a platform secret can override a
    stale value baked into an image."""
    cfg = {}
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        cfg = dict(l.strip().split("=", 1) for l in open(p)
                   if "=" in l and not l.strip().startswith("#"))
    for k in KEYS:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg.setdefault("MDS_BASE_URL", "https://fleet-green.moveinsync.com")
    if not cfg.get("MDS_USERNAME") or not cfg.get("MDS_PASSWORD"):
        sys.exit("MDS_USERNAME / MDS_PASSWORD not set (via .env or environment)")
    return cfg


class MDS:
    def __init__(self, env=None):
        self.env = env or load_env()
        self.base = self.env["MDS_BASE_URL"].rstrip("/")
        self._tok = None
        self._issued = 0
        self._load_cache()

    # ─────────────────────────── auth ───────────────────────────
    def _load_cache(self):
        try:
            c = json.load(open(TOKEN_CACHE))
            if time.time() - c["issued"] < TOKEN_TTL_S - REFRESH_MARGIN_S:
                self._tok, self._issued = c["tok"], c["issued"]
        except Exception:
            pass

    def _save_cache(self):
        os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
        json.dump({"tok": self._tok, "issued": self._issued}, open(TOKEN_CACHE, "w"))
        os.chmod(TOKEN_CACHE, 0o600)

    def login(self):
        body = {"username": self.env["MDS_USERNAME"], "password": self.env["MDS_PASSWORD"]}
        r = self._raw("POST", "/fis/auth/login", body, auth=False)
        # login signals failure in the BODY, not the status code
        if not r or not r.get("successStatus"):
            raise RuntimeError("MDS login failed: %s" % (r or {}).get("message"))
        d = r["data"]
        self._tok = {"Authorization": d["token"], "x-cds-token": d["token"],
                     "user_detail": d["userDetailToken"], "vendor_id": str(d["vendorId"])}
        self._issued = time.time()
        self._save_cache()
        return d

    def headers(self):
        if not self._tok or time.time() - self._issued > TOKEN_TTL_S - REFRESH_MARGIN_S:
            self.login()
        return dict(self._tok)

    # ─────────────────────────── transport ───────────────────────────
    def _raw(self, method, path, body=None, auth=True, timeout=90):
        cmd = ["curl", "-s", "-o", "/dev/stdout", "-w", "\n__HTTP__%{http_code}",
               "-X", method, "-H", "Accept: application/json, text/plain, */*",
               "-H", "Referer: %s/cds-green.html" % self.base]
        if auth:
            for k, v in self.headers().items():
                cmd += ["-H", "%s: %s" % (k, v)]
        if body is not None:
            cmd += ["-H", "Content-Type: application/json", "--data-raw", json.dumps(body)]
        cmd += ["--max-time", str(timeout), self.base + path]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        payload, _, code = out.rpartition("__HTTP__")
        try:
            return json.loads(payload.strip() or "{}") if code.strip() != "401" else None
        except Exception:
            return None

    def call(self, method, path, body=None, _retried=False):
        """Data endpoints use a real 401 -> re-login once and retry."""
        r = self._raw(method, path, body)
        if r is None and not _retried:
            self.login()
            return self.call(method, path, body, _retried=True)
        return r

    # ─────────────────────────── endpoints ───────────────────────────
    def vendor_guids(self, buid):
        r = self.call("GET", "/fis/vendor/vendorowner/mapping/assigned")
        for m in r["data"]["vendorOwnerMappings"]:
            if m.get("buid") == buid:
                return [v["vendorGuid"] for v in m["assignedVendors"]]
        raise RuntimeError("no vendor mapping for %s" % buid)

    def shifts(self, buid, guids, day_ms):
        r = self.call("POST", "/fis/cds/shift/v3?nonShift=false&isPlannedTrip=false",
                      {"dates": [day_ms],
                       "buidsAndVendors": [{"businessUnit": buid, "vendorGuids": guids}]})
        out = []
        for d in (r or []):
            for b in d.get("businessUnitOfficeShifts", []):
                for o in b.get("officeShifts", []):
                    out.append(o)
        return out

    def trips(self, buid, guids, day_ms, offices=None):
        """Trip list. MDS caps this at 10 shifts per call, so batch.
        The 'shiftsByOfficeDirectionByBuid' shape is NOT a selectedShifts list —
        it's {buid: [{office:{name,id}, shiftsByDirection:{loginShifts,logoutShifts}}]}
        and `directions` must match whichever list is populated."""
        all_trips, seen = [], set()
        for off in self.shifts(buid, guids, day_ms):
            if offices and off["officeName"] not in offices:
                continue
            for direction, key in (("IN", "loginShifts"), ("OUT", "logoutShifts")):
                times = [s["shiftTime"] for s in (off.get(key) or [])]
                for i in range(0, len(times), 10):
                    chunk = times[i:i + 10]
                    body = {
                        "dateList": [day_ms, day_ms],
                        "businessUnitVendorsList": [{"businessUnit": buid, "vendorGuids": guids}],
                        "directions": [direction],
                        "shiftsByOfficeDirectionByBuid": {buid: [{
                            "office": {"name": off["officeName"], "id": off["officeId"]},
                            "shiftsByDirection": {
                                "loginShifts":  chunk if direction == "IN" else [],
                                "logoutShifts": [] if direction == "IN" else chunk}}]},
                        "selectedShifts": [{"buid": buid, "office": off["officeName"],
                                            "shift": t, "officeId": off["officeId"],
                                            "direction": direction} for t in chunk],
                        "plannedTrip": False}
                    r = self.call("POST",
                                  "/fis/cds/trip/filter?source=web&nonShift=false&cabUnassigned=false",
                                  body)
                    for t in ((r or {}).get("data") or []):
                        if t["tripId"] not in seen:
                            seen.add(t["tripId"]); all_trips.append(t)
        return all_trips

    def vehicles(self, buid, trip_guid):
        """Candidate pool. distance=true is REQUIRED: with distance=false,
        emptyLegInMetres is -1 for every cab."""
        g = urllib.parse.quote(trip_guid, safe="$-")
        r = self.call("GET", "/fis/cds/vendor/trips/%s/%s/vehicles?distance=true" % (buid, g))
        return (r or {}).get("data") or []

    @staticmethod
    def trip_guid(buid, trip_id):
        """Best-effort GUID construction, so a trip is reachable without trip/filter.
        Rule: strip dashes from the buid, chunk into 4s, join with dashes, then
        $ + zero-padded-10 trip id.
          ivycomptech-IVYHyd -> ivycomptechIVYHyd -> ivyc|ompt|echI|VYHy|d
          + 1135587 -> TRIPivyc-ompt-echI-VYHy-d$0001135587
        Verified for ivycomptech-IVYHyd only. Prefer the tripGuid returned by
        trips() — this is a convenience fallback, not the primary path."""
        s = buid.replace("-", "")
        chunks = [s[i:i + 4] for i in range(0, len(s), 4)]
        return "TRIP" + "-".join(chunks) + "$%010d" % int(trip_id)


if __name__ == "__main__":
    m = MDS()
    d = m.login()
    print("logged in as %s (userId %s, vendorId %s)" % (d["name"], d["userId"], d["vendorId"]))
    buid = sys.argv[1] if len(sys.argv) > 1 else "ivycomptech-IVYHyd"
    g = m.vendor_guids(buid)
    print("%d vendors for %s" % (len(g), buid))
    day = int(datetime.combine(datetime.now().date(), datetime.min.time()).timestamp() * 1000)
    t = m.trips(buid, g, day)
    print("%d trips today, %d unassigned" % (len(t), sum(1 for x in t if not x.get("cabAssigned"))))
