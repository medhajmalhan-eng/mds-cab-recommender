#!/usr/bin/env python3
"""
Container entrypoint: one process that owns the whole service.

A VM gets its schedule from crontab and its supervision from systemd. A
container has neither, so this does three things in order:

  1. `sync.py --ensure` before serving — seeds the history on a cold start and
     fills any gap left by downtime. Idempotent: a warm volume skips it in
     seconds.
  2. A daily scheduler thread that runs the sync, then the shadow reconcile.
     Default 22:00 IST — deliberately NOT after midnight, because deployers
     work past midnight on early-morning shifts and a 00:30 run would leave
     them a day staler.
  3. The HTTP service in the foreground, so the platform's own restart policy
     supervises it.

Env: everything service.py/sync.py read, plus
     SYNC_AT_HHMM   local time for the nightly job (default 2200)
     TZ             set to Asia/Kolkata in the image
"""
import os, subprocess, sys, threading, time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = os.environ.get("PORT", "8770")
SYNC_AT = os.environ.get("SYNC_AT_HHMM", "2200")


def run(label, args):
    t0 = time.time()
    print(f"[{label}] start", flush=True)
    p = subprocess.run([sys.executable, *args], cwd=ROOT,
                       capture_output=True, text=True)
    for line in (p.stdout or "").splitlines()[-25:]:
        print(f"[{label}] {line}", flush=True)
    if p.returncode:
        print(f"[{label}] FAILED rc={p.returncode}: "
              f"{(p.stderr or '').strip()[:400]}", flush=True)
    print(f"[{label}] done in {time.time() - t0:.0f}s", flush=True)
    return p.returncode


def seconds_until(hhmm):
    now = datetime.now()
    target = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]),
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def scheduler():
    while True:
        time.sleep(seconds_until(SYNC_AT))
        run("sync", ["sync.py"])
        run("reconcile", ["shadow.py", "reconcile"])
        time.sleep(60)          # don't re-fire inside the same minute


if __name__ == "__main__":
    # Cold start on an empty volume pulls the full window (~7 min); a warm one
    # is a no-op. Either way we do not serve stale-or-empty recommendations.
    run("ensure", ["sync.py", "--ensure"])
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"[run] nightly sync scheduled for {SYNC_AT[:2]}:{SYNC_AT[2:]} "
          f"({os.environ.get('TZ', 'container TZ')})", flush=True)
    os.execv(sys.executable, [sys.executable, os.path.join(ROOT, "service.py"), PORT])
