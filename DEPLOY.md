# Deploying this

There are two things in this repo:

| | what it is | where it runs |
|---|---|---|
| **`web/`** | the deployable screen | **Netlify, free tier** |
| root `*.py` | the research code that established the scoring — backtests, sweeps, accuracy measurement | your laptop, or a VM if you want it running nightly |

**If you just want deployers using it tomorrow, you only need `web/`.** Skip to
"Deploying web/ on Netlify".

---

## Correcting an earlier claim in this file

This document used to open with *"Why Netlify won't work"* and recommend a
container. That was wrong, and worth being explicit about because the reasoning
looked sound:

> - a long-running Python process
> - stateful — an 88 MB SQLite file that must survive restarts
> - a nightly cron that appends to that same database

Every line is true of `service.py`. None of it is true of *the recommendation
screen*. I had confused how the thing was **built** with what it needs to **run**:

- The 88 MB database is 30 days of history in a format convenient for SQL. The
  scorer reads five fields per trip; re-expressed that way the largest BU is
  **196 KB gzipped**, and all 32 fit in 1.4 MB.
- The "long-running process" was a cache warmer and the accuracy sweeper. The
  cache is a nicety; the sweeper is measurement, not the product.
- The nightly cron is a build step. Netlify runs those.

What genuinely needs a server is the **measurement** — predicting overnight and
reconciling against what deployers chose. That is a real thing to want, and it
is why the Python side still exists. It just is not a prerequisite for anyone
using the screen.

---

## Deploying `web/` on Netlify

A separate site from the Virtual Cab Finder — different audience, different
deploy cadence, and a failed build in one should not take down the other.

### 1. Push the repo

```bash
cd ~/mds-cab-recommender
git add -A && git commit -m "Web port of the recommender"
gh repo create mds-cab-recommender --private --source=. --push
```

`data/`, `.env` and `*.har` are gitignored. Nothing secret and nothing large
goes up — check with `git status` before the first push if you want to be sure.

### 2. Create the site

Netlify → **Add new site → Import an existing project** → pick the repo.

Netlify reads `netlify.toml`, which already sets base `web`, publish
`web/public`, and the function directory. **Do not override these in the UI** —
a value typed into the dashboard silently wins over the file.

### 3. Environment variables

Site configuration → Environment variables. All five, scope **All scopes**:

| variable | what it is |
|---|---|
| `APPS_SCRIPT_EXEC_URL` | the `/exec` URL of the Metabase proxy |
| `APPS_SCRIPT_TOKEN` | its shared token |
| `MDS_EMAIL` | your MDS login (`MDS_USERNAME` also accepted) |
| `MDS_PASSWORD` | its password |
| `SITE_PASSWORD` | the shared password for the screen itself — pick one |

The same values are in your local `.env`, except `SITE_PASSWORD` which is new
(`UI_PASSWORD` was its Python equivalent).

Two things that cost time on the VCF site and will cost it again here:

- **Scope must include Functions.** A Builds-only variable produces a working
  build and a function that cannot see it.
- **Functions capture variables at deploy time.** Changing one does nothing
  until you redeploy.

### 4. Deploy

The build fetches 30 days from Metabase — about four minutes, most of it
waiting on Apps Script. It **fails loudly** if the credentials are missing
rather than publishing a screen whose data all 404s.

### 5. Keep it fresh

The history is baked at build time, so with no schedule it freezes at whatever
day you last deployed.

Build & deploy → **Build hooks** → create one, then trigger it nightly. The
simplest scheduler is a GitHub Action in this repo:

```yaml
# .github/workflows/nightly.yml
name: nightly rebuild
on:
  schedule: [{ cron: '40 17 * * *' }]   # 23:10 IST — after the day's trips settle
  workflow_dispatch:
jobs:
  rebuild:
    runs-on: ubuntu-latest
    steps:
      - run: curl -fsS -X POST -d '{}' "${{ secrets.NETLIFY_BUILD_HOOK }}"
```

Put the hook URL in the repo's Settings → Secrets as `NETLIFY_BUILD_HOOK`.

---

## What the deployed screen does and does not do

**Does:** everything a deployer touches — the wave, Layer 1 and Layer 2
recommendations, live feasibility against MDS, and for already-assigned trips,
where the deployer's actual pick ranked.

**Does not:** the accuracy sweeper, shadow logging, and reconciliation. Those
need a process that runs while nobody is watching. Losing them costs no
functionality; it costs the ability to say *"we agreed with deployers X% of the
time last week"* from live use. The backtest number (26.6% top-1 / 45.5% top-3 /
55.2% top-5 over 41,203 trips) still stands — it just stops being updated.

If you want that back later it is a scheduled job, not a server: the same
prediction, written to a store, reconciled the next morning.

---

## Before real deployers use it

1. **The screen is behind one shared password**, not identity. It keeps the
   history off a public URL — which matters, because the shards contain
   `anchor_pickup_geo`, the precise coordinate where each route's first employee
   is collected. It does not tell you who looked at what. For a pilot that is a
   reasonable trade; if it outlives the pilot, put real SSO in front of it.

2. **Everything runs as your personal MDS login.** Your next password change
   breaks it for everyone, silently, until someone updates `MDS_PASSWORD`. A
   service account for the CDS APIs is the fix and is worth asking for early.

3. **Watch MDS rate limits.** Each open screen polls the wave every 30 s, and
   each trip click fetches one vehicle pool (Layer 1) or one per vendor
   (Layer 2). Fine for a handful of deployers; untested beyond that, and it is
   all one account.

4. **Where the data lives is still worth raising.** The history sits in a
   Netlify function bundle on infrastructure your org does not govern. The
   password gate is a mitigation, not an answer. Worth a conversation with
   whoever owns data governance before this becomes load-bearing.

---

## Running the Python side (optional)

Only needed for measurement and for re-running the backtest.

```bash
cd ~/mds-cab-recommender
cp .env.example .env && $EDITOR .env
python3 sync.py --backfill      # ~7 min, builds data/history.db
python3 service.py              # http://127.0.0.1:8770
python3 backtest.py             # the accuracy numbers quoted above
```

For a nightly measurement loop this does want an always-on host — an internal VM
is the right home for it, since it holds your MDS password and the extract
contains employee geocodes. `bootstrap.sh` sets up systemd and the cron.

---

## Keeping the two scorers honest

`web/public/scorer.js` is a hand port of `recommend.py`. The backtested accuracy
belongs to the Python; the JavaScript is what deployers actually see. If they
drift, recommendations quietly stop matching what was measured and nothing
anywhere reports an error.

So before deploying a scoring change:

```bash
python3 verify_fixture.py ivycomptech-IVYHyd 10 > web/fixture.json
node web/scripts/verify.mjs web/fixture.json
```

This captures real trips and real candidate pools, scores them with both
implementations, and requires identical output — same cabs, same order, same
scores to 1e-9, same rejection reasons. It must print `0 failed`.
