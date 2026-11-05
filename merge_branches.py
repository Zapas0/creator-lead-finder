#!/usr/bin/env python3
"""
merge_branches.py - fold the databases of other remote branches into this clone's leads.db.

Earlier scheduled sessions each pushed to their own branch and never merged, so every one
started from a database that lacked the others' work. pilot.py run calls merge_all() after it
takes the run lock and before daily.py, so each run starts from the union of everything found.

Safe to run repeatedly (INSERT OR IGNORE; nothing is ever removed). Not merged: `meta`
(quota counters and search bookmarks belong to this branch's own quota days).
Also copies other branches' dated run logs into logs/<day>.<branch>.log if missing.
"""
import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KEYED = ["checked", "manual", "manual_done", "snowball_done", "exported", "delivered"]


def git(*a):
    return subprocess.run(["git", *a], cwd=HERE, capture_output=True)


def remote_branches():
    cur = git("rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
    refs = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").stdout.decode().split()
    return [r for r in refs if r not in ("origin", "origin/HEAD", f"origin/{cur}")]


def merge_all(db_path, state_rel="state/leads.db.gz"):
    git("fetch", "--quiet", "origin")
    con = sqlite3.connect(db_path)
    have = {r[0] for r in con.execute("SELECT channel_id FROM leads")}
    emails = set()
    for (d,) in con.execute("SELECT data FROM leads"):
        emails.add(json.loads(d).get("email", "").lower())
    report = []
    for ref in remote_branches():
        blob = git("show", f"{ref}:{state_rel}")
        if blob.returncode or not blob.stdout:
            continue
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "other.db")
            with open(p, "wb") as f:
                f.write(gzip.decompress(blob.stdout))
            o = sqlite3.connect(p)
            added = {"leads": 0, "skipped_dup_email": 0}
            for cid, found, data in o.execute("SELECT channel_id, found_at, data FROM leads"):
                if cid in have:
                    continue
                em = json.loads(data).get("email", "").lower()
                if em in emails:
                    added["skipped_dup_email"] += 1
                    continue
                con.execute("INSERT OR IGNORE INTO leads VALUES (?,?,?)", (cid, found, data))
                have.add(cid)
                emails.add(em)
                added["leads"] += 1
            for t in KEYED:
                try:
                    rows = o.execute(f"SELECT * FROM {t}").fetchall()
                except sqlite3.Error:
                    continue
                if rows:
                    q = ",".join("?" * len(rows[0]))
                    n0 = con.total_changes
                    con.executemany(f"INSERT OR IGNORE INTO {t} VALUES ({q})", rows)
                    added[t] = con.total_changes - n0
            try:
                rows = o.execute("SELECT * FROM pending").fetchall()
                con.executemany("INSERT OR IGNORE INTO pending VALUES (?,?,?)", rows)
            except sqlite3.Error:
                pass
            o.close()
        report.append(f"{ref}: {added}")
    # anything already checked/leads/manual is not pending
    con.execute("DELETE FROM pending WHERE channel_id IN (SELECT channel_id FROM checked)")
    con.commit()
    con.close()
    # copy missing logs of other branches
    for ref in remote_branches():
        names = git("ls-tree", "--name-only", f"{ref}", "logs/").stdout.decode().split()
        for n in names:
            b = os.path.basename(n)
            if len(b) == 14 and b.endswith(".log") and b[:2] == "20" and b[4] == "-":
                dest = os.path.join(HERE, "logs", f"{b[:-4]}.{ref.split('-')[-1]}.log")
                if not os.path.exists(dest) and not os.path.exists(os.path.join(HERE, "logs", b)):
                    data = git("show", f"{ref}:{n}").stdout
                    if data:
                        with open(dest, "wb") as f:
                            f.write(data)
                        report.append(f"log copied: {os.path.basename(dest)}")
    return report


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    import creator_leads as cl
    cfg = cl.load_config(os.path.join(HERE, "config.json"))
    print("\n".join(merge_all(cfg["db_path"])))
