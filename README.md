# MDS Cab Recommender

A companion screen for MDS deployers. Given an unassigned trip, it recommends
which cab to deploy — based on which cabs have actually done similar trips in the
last 30 days, filtered to those that can physically do it right now.

Scope: Hyderabad. Built and validated against production, Aug 2026.

---

## Run it

```bash
python3 service.py            # http://127.0.0.1:8770
```

Open <http://127.0.0.1:8770> next to the MDS tab.

Nightly jobs (macOS needs Full Disk Access for cron):

```bash
(crontab -l 2>/dev/null; cat data/crontab.txt) | crontab -
```

---

## How it works

```
Metabase card 130776 ──▶ Apps Script /exec ──▶ sync.py ──▶ SQLite (30d rolling)
                                                              │
MDS live APIs ─── trip list + candidate pool ───▶ recommend.py ┘──▶ ranked cabs
```

History is precomputed nightly because scoring must be sub-second; a Metabase
card takes 4–8 s. The live candidate pool and feasibility come from MDS per
request.

| file | role |
|---|---|
| `sync.py` | Metabase → SQLite. `--backfill` (resumable), `--day`, default = append yesterday + prune + rebuild profiles |
| `mds.py` | MDS client: auth+refresh, vendor guids, shifts, trips, candidate pool |
| `recommend.py` | scoring, feasibility, evidence, vendor-shortfall detection |
| `service.py` | HTTP: `/`, `/wave`, `/recommend`, `/health` |
| `shadow.py` | logs every recommendation, reconciles against what the deployer did |
| `appscript/Code.gs` | standalone Metabase proxy (holds the service-account auth) |
| `sql/history_extract.sql` | the extract, saved as Metabase card 130776 |

---

## The scoring model

Frozen after backtesting **41,203 trips** across 6 Hyderabad BU-offices, rolling
30-day window, no leakage. Measured against what deployers actually did:

| | top-1 | top-3 | top-5 |
|---|---|---|---|
| overall | 26.6% | 45.5% | 55.2% |
| LOGIN, first trip of day | 39.3% | — | 68.8% |

```
hard filters  → exact-route tier → fallback kernel → reliability tiebreak
```

* **hard filters** — vendor (via which pool), capacity, compliance, active,
  not virtual, not already deployed in this wave, time+space feasible
* **exact-route tier** — history within 1 km + same shift, recency-weighted.
  Always outranks the fallback.
* **fallback kernel** — `exp(-d/3km)` capped at 10 km, ×3 on a shift match,
  21-day recency half-life, divided by `n_cab^0.5` (specificity: a cab that
  *only* works this area beats a cab that works everywhere)
* **anchor** — the `planned_emp_order = 0` employee's pickup geo (LOGIN) or
  drop geo (LOGOUT); that row is the employee farthest from the office

### Tested and rejected — do not re-add

| idea | result |
|---|---|
| employee ↔ cab affinity | < 1 pt; routes are re-pooled daily |
| window > 30 days | top-1 flat; only +1 pt top-5 |
| greedy global assignment | 20.4% vs 24.2% — cabs legitimately do 3–5 trips/day |
| reliability as a ranking term | 49.3% → 49.1%; kept as a tiebreak only |
| garage anchor applied globally | helps LOGIN-first only |
| offline state/chaining features | +0.1 pt — the offline position proxy is too weak |

The ceiling is ~99%, not 82%: of the trips we miss, 95% are cabs *with* history
at that site, just >3 km from the anchor. This is a ranking problem, not a
reach problem.

---

## Two layers

**Layer 1 — same vendor.** Imitate the deployer. The MDS candidate pool is
already vendor-scoped, so this is just the trip's own pool.

**Layer 2 — any vendor.** For when a vendor says *"I've got 10 trips and 5
cabs."* Same scoring logic — vendor is **not** a scoring term, it only decides
which pool goes in. Cross-vendor needs one pool per vendor in the wave (~6
calls) because the endpoint has no vendor parameter.

`emptyLegInMetres` is computed by MDS for whichever trip was queried, so it is
**wrong** for cross-vendor candidates and is suppressed rather than shown.

Measured: pool 50 → 191 cabs, evidence density 1.9/5 → 2.7/5 backed by real
route history.

---

## Feasibility

| check | source | quality |
|---|---|---|
| cab → this pickup | `emptyLegInMetres` | exact, MDS-computed |
| prev drop → this pickup | trip/filter geocodes | **full** — real distance ÷ 25 km/h |
| this drop → next pickup | trip/filter geocodes | **full** |
| commitments in other BUs | `vehicleNext/LastTripDetails` | **time-only** — no location in the API |

Every recommendation reports which depth it got. `garageLocation` and
`lastTripLocation` exist in the API but are never populated, so the garage anchor
is derived from history instead (median first-LOGIN-of-day pickup per cab).

---

## Open question

The exact-route tier always outranks the fallback — but it was fitted on
history, where `emptyLegInMetres` does not exist. Live, that produces things
like *#1 did this route once but is 27 km away; #2 has 33 area trips and is
1.9 km away*, and a deployer would very likely take #2.

Do not guess a weight. `shadow.py report` measures it directly:

```bash
python3 shadow.py report
```

---

## Gotchas worth knowing

* `Authorization: <raw token>` — no `Bearer`. Chrome's HAR export **strips**
  this header, so it's invisible in `.har` files and only shows in Copy as cURL.
  Without it: empty-body 401 that looks like a bad token.
* `/fis/auth/login` returns **HTTP 200 on failure** with the error in
  `successStatus`. Data endpoints use a real 401. Two different checks needed.
* `trip/filter` caps at **10 shifts per call** — batch, or you get a 500.
* `?distance=true` is mandatory on the vehicles endpoint, or
  `emptyLegInMetres` is `-1` for every cab.
* `(trip_id, bunit_id)` is the unique key. `trip_id` alone is not.
* Trip GUIDs are constructible: buid minus dashes, chunked into 4s, then
  `$` + zero-padded-10 trip id.
* Weekends run ~10% of weekday volume, so 30 days ≈ 22 days of real history.
* Sync at **22:00, not after midnight** — deployers work past midnight on
  early-morning shifts.

---

## Not done yet

* Prefetching candidate pools for a whole wave (~1.25 s per trip today)
* Vendor-shortfall surfacing in the UI (backend exists: `vendor_shortfall()`)
* A service account for the CDS APIs — this currently runs on a personal
  password, which is fine for a pilot and not for production
