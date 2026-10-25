#!/usr/bin/env python3
"""
pilot.py - the one entry point agents use. Safe when several agents share this repo.

    python3 pilot.py status                   # pull, show progress, quota and lock (no API calls)
    YT_API_KEY=... python3 pilot.py run       # pull, take the run lock, run daily.py,
                                              # commit + push results, release the lock
    python3 pilot.py release                  # remove a lock this clone holds, or an expired one

The run lock (run.lock, committed and pushed) makes sure only one agent runs at a time:
the API quota is shared, and state/leads.db.gz can't be merged. Taking the lock is a git
push, so if two agents try at once, one push is rejected and that agent backs off.
A lock expires after LOCK_HOURS, so a crashed agent can't block the others for long.

Exit codes: 0 done, 3 target reached, 4 another agent holds the lock,
            1 the run hit an error (see logs/), 2 setup/git problem.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, "run.lock")
LOCK_HOURS = 3
AGENT_FILE = os.path.join(HERE, ".agent_id")          # this clone's identity (gitignored)
SHARED = ("state", "output", "logs")                  # only the lock holder commits these


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)
    if check and r.returncode:
        print(f"GIT ERROR: git {' '.join(args)}\n{(r.stderr or r.stdout).strip()}")
        sys.exit(2)
    return r


def ensure_identity():
    if not git("config", "user.email", check=False).stdout.strip():
        git("config", "user.email", "creator-lead-finder@users.noreply.github.com")
    if not git("config", "user.name", check=False).stdout.strip():
        git("config", "user.name", "Creator Lead Finder agent")


def agent_id():
    if not os.path.exists(AGENT_FILE):
        with open(AGENT_FILE, "w") as f:
            f.write(f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}")
    return open(AGENT_FILE).read().strip()


def branch():
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def pull():
    git("pull", "--rebase", "--autostash", "--quiet", "origin", branch())


def read_lock():
    if not os.path.exists(LOCK):
        return None
    try:
        return json.load(open(LOCK))
    except ValueError:
        return {"agent": "unknown", "expires_at": iso(now() - timedelta(seconds=1))}


def active(lock):
    return bool(lock) and parse(lock["expires_at"]) > now()


def push_with_retry(tries=4):
    for _ in range(tries):
        if git("push", "origin", branch(), check=False).returncode == 0:
            return True
        r = git("pull", "--rebase", "origin", branch(), check=False)
        if r.returncode:
            git("rebase", "--abort", check=False)
            print("GIT ERROR: could not rebase onto the remote. Someone changed the same files; "
                  "resolve by hand (state/, output/, logs/ belong to the lock holder).")
            return False
    return False


def claim(me, day):
    """Take the lock with a commit + push. Returns (True, lock) or (False, holder's lock)."""
    pull()
    held = read_lock()
    if active(held) and held.get("agent") != me:
        return False, held
    lock = {"agent": me, "quota_day": day, "started_at": iso(now()),
            "expires_at": iso(now() + timedelta(hours=LOCK_HOURS))}
    with open(LOCK, "w") as f:
        json.dump(lock, f, indent=2)
    git("add", "run.lock")
    git("commit", "-q", "-m", f"Take run lock for quota day {day} ({me})")
    if git("push", "origin", branch(), check=False).returncode == 0:
        return True, lock
    # someone pushed first: drop our lock commit and look at theirs
    git("reset", "-q", "--hard", "HEAD~1")
    pull()
    held = read_lock()
    if active(held) and held.get("agent") != me:
        return False, held
    return claim(me, day)


def release_and_commit(message):
    """Commit the run's results together with removing the lock, then push."""
    if os.path.exists(LOCK):
        os.remove(LOCK)
    git("add", "-A", "--", *[p for p in SHARED if os.path.exists(os.path.join(HERE, p))])
    git("add", "-A", "--", "run.lock")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return True
    git("commit", "-q", "-m", message)
    return push_with_retry()


def status_text():
    out = subprocess.run([sys.executable, os.path.join(HERE, "daily.py"), "--status"],
                         cwd=HERE, capture_output=True, text=True).stdout.strip()
    lock = read_lock()
    if active(lock):
        out += (f"\nRun lock: held by {lock['agent']} since {lock['started_at']} "
                f"(expires {lock['expires_at']})")
    elif lock:
        out += f"\nRun lock: stale lock from {lock.get('agent')} (expired {lock.get('expires_at')})"
    else:
        out += "\nRun lock: free"
    prog = os.path.join(HERE, "output", "progress.csv")
    if os.path.exists(prog):
        lines = open(prog, encoding="utf-8").read().strip().splitlines()
        out += "\nRecent runs (output/progress.csv):\n  " + "\n  ".join(lines[:1] + lines[1:][-5:])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["status", "run", "release"])
    args = ap.parse_args()
    sys.path.insert(0, HERE)
    import creator_leads as cl
    ensure_identity()
    me = agent_id()

    if args.command == "status":
        if not git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            pull()
        print(status_text())
        return 0

    if args.command == "release":
        pull()
        lock = read_lock()
        if not lock:
            print("No lock to release.")
            return 0
        if lock.get("agent") != me and active(lock):
            print(f"The lock belongs to {lock.get('agent')} and is active until {lock.get('expires_at')}; "
                  "not touching it.")
            return 4
        ok = release_and_commit(f"Release run lock ({me})")
        print("Lock released." if ok else "Could not push the release.")
        return 0 if ok else 2

    # ---- run ----
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("SETUP ERROR: this clone has uncommitted changes to tracked files. "
              "Commit (and push) or stash them first, so the run commit only contains run results.")
        return 2
    if not os.environ.get("YT_API_KEY"):
        print("SETUP ERROR: set the YT_API_KEY environment variable.")
        return 2
    day = cl.youtube_quota_day()
    ok, lock = claim(me, day)
    if not ok:
        print(f"ANOTHER RUN IN PROGRESS: {lock.get('agent')} holds the lock since {lock.get('started_at')} "
              f"(expires {lock.get('expires_at')}). Don't run; check again later with `pilot.py status`.")
        return 4
    print(f"Lock taken by {me} for quota day {day}. Running daily.py (usually 40-70 minutes)...", flush=True)
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    proc = subprocess.run([sys.executable, "-u", os.path.join(HERE, "daily.py")], cwd=HERE,
                          capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    with open(os.path.join(HERE, "logs", "pilot_last.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    print(out.strip())
    summary = {}
    for line in out.splitlines():
        if line.startswith("SUMMARY "):
            summary = json.loads(line[len("SUMMARY "):])
    if summary:
        msg = (f"Run {summary['quota_day']}: +{summary['new_leads_today']} leads, "
               f"{summary['channels_checked_total']:,} channels checked, status {summary['status']}")
    elif "ALREADY RAN" in out:
        msg = f"Quota day {day}: already ran, nothing to do"
    elif "TARGET REACHED" in out:
        msg = f"Quota day {day}: target reached"
    else:
        msg = f"Quota day {day}: run ended without a summary (exit {proc.returncode})"
    pushed = release_and_commit(msg + f" ({me})")
    print(("Results pushed and lock released: " if pushed else "PUSH FAILED (lock still in the repo): ") + msg)
    if not pushed:
        return 2
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
