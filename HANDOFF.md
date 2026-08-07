# Cab Recommender — guide for site leads & deployers

## What this is

A companion screen to MDS. Keep MDS open in one tab and this in another.
It suggests **which cab to deploy** for each open trip, based on which cabs
actually did similar trips in the last 30 days *and* can physically do this one
right now (no clash with their other trips, close enough to reach the pickup).

It is **read-only**: it never assigns anything. You still deploy in MDS.
Nothing you click here changes anything in MDS.

## Opening it

1. Open the URL you were given. Log in with **any username** + the shared password.
2. Pick your **site**, the **date** (tomorrow works — for midnight deployment of
   early-morning logins), and optionally a **shift wave**.
3. Click any open trip → the right panel shows the top 5 cabs.

The list refreshes every 30 seconds, so cabs you deploy in MDS disappear from
suggestions for that wave on their own. A red cut-off time means the assignment
cut-off has passed.

## Reading a recommendation card

```
1. TS-05-UF-1370                     DCO MIS
   did this route 13× in 30d (nearest 0.0 km) · 4.8 km away now
   [strong] [13× exact route] [4.8 km away] [full check]
```

- **strong** (green edge) — this cab has done this exact route recently. Trust it.
- **medium** (amber) — it works this area, but not this exact route.
- **weak** (grey) — no useful history; it's merely nearby and free. Judge yourself.
- **km away** — distance from the cab's current position to this pickup.
- **full check** — we verified its whole day: previous drop → this pickup → its
  next trip, with travel time. **time-only check** — part of its day is with a
  client we can't see, so only the clock was compared. Trust your knowledge there.
- **⚠ driver-fault** — this cab caused delays on 10%+ of its recent trips.

Cabs that *can't* do the trip (too far, busy, would miss their next trip,
already used in this wave, non-compliant, wrong capacity) are removed before
you ever see them.

## Same vendor vs Any vendor

- **Same vendor** (default) — cabs of the vendor already on the trip.
- **Any vendor** — for when a vendor says *"I have 10 trips but only 5
  vehicles"*: shows the best cabs across ALL vendors so you know where to move
  the orphaned trips. Distances are hidden here because they can't be computed
  exactly across vendors.
- If a trip has **no vendor yet**, it shows all vendors automatically.

## What to expect — honestly

Tested against 5,614 real Ivy deployments: our #1 matches what the deployer
chose ~30% of the time on trips deployed before cut-off, and the deployer's pick
is somewhere in our top-5 ~60% of the time. Regular morning waves (09:00, 10:00)
match best; irregular waves (17:30, 18:30) much less — expect more *medium* and
*weak* cards there, and lean on your own judgement.

The tool is a memory aid, not an authority. When you disagree with it, you're
probably right — and your choice is recorded (automatically, via MDS) and used
to make it better.

## When something looks wrong

| symptom | meaning / fix |
|---|---|
| password prompt loops | wrong password — check with the owner |
| "MDS login failed" banner | the service's MDS password expired — tell the owner |
| trip missing from the list | Refresh; check date & site selectors |
| "recommendations are stale" note | that trip just got assigned in MDS |
| empty recommendations | no feasible cab passed the checks — genuine scarcity |

Owner: Medhaj (medhaj.malhan@moveinsync.com).
