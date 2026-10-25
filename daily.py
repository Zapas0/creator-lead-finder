#!/usr/bin/env python3
"""
daily.py - one daily run of the Creator Lead Finder, start to finish.
Agents: don't call this directly; use `python3 pilot.py run`, which adds the run lock
and commits the results.

    YT_API_KEY=... python3 daily.py            # one run
    python3 daily.py --status                  # print progress only, no API calls

What it does:
  1. makes leads.db match state/leads.db.gz (re-extracts it if the state file changed)
  2. stops early if the email target is reached or today's quota is already spent
  3. runs creator_leads.py (98 searches + 9,800 units max per YouTube quota day),
     logging to logs/<quota day>.log
  4. writes output/emails_<day>.csv and output/to_reveal_<day>.csv (only what is new that day),
     saves the database back to state/leads.db.gz and appends output/progress.csv
  5. writes logs/last_summary.json and prints a SUMMARY line

Exit codes: 0 = ran or nothing to do today, 3 = target reached (stop the schedule),
            1 = the run hit an error (see the log), 2 = setup problem (no key, no database).
"""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import creator_leads as cl  # noqa: E402

PROGRESS = os.path.join(HERE, "output", "progress.csv")
SUMMARY = os.path.join(HERE, "logs", "last_summary.json")
PROGRESS_FIELDS = ["quota_day", "emails_total", "channels_checked_total", "channels_checked_today", "leads_total",
                   "new_leads_today", "manual_waiting", "searches_used", "units_used",
                   "search_results_new_pct", "status"]


def stats(con, day):
    def one(sql, *a):
        r = con.execute(sql, a).fetchone()
        return r[0] if r and r[0] is not None else 0
    purged = int(one("SELECT value FROM meta WHERE key='purged_checked'") or 0)
    return {
        "checked_total": one("SELECT COUNT(*) FROM checked") + purged,
        # every unique email ever found (hashes are kept forever; leads expire after 30 days)
        "emails_total": one("SELECT COUNT(*) FROM exported"),
        "leads_total": one("SELECT COUNT(*) FROM leads"),
        "manual_waiting": one("SELECT COUNT(*) FROM manual WHERE channel_id NOT IN "
                              "(SELECT channel_id FROM manual_done)"),
        "pending": one("SELECT COUNT(*) FROM pending"),
        "searches_used": int(one("SELECT value FROM meta WHERE key=?", f"searches_{day}") or 0),
        "units_used": int(one("SELECT value FROM meta WHERE key=?", f"units_{day}") or 0),
    }


def write_progress(row):
    os.makedirs(os.path.dirname(PROGRESS), exist_ok=True)
    rows = []
    if os.path.exists(PROGRESS):
        with open(PROGRESS, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("quota_day") != row["quota_day"]]
    rows.append(row)
    rows.sort(key=lambda r: r["quota_day"])
    with open(PROGRESS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PROGRESS_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# Everything found up to this time counts as already delivered; daily files start after it.
DELIVERED_BASELINE_AT = "2000-01-01T00:00:00Z"
DATED_OUTPUTS = re.compile(r"^(emails|to_reveal|leads|manual_check|revealed)_(\d{4}-\d{2}-\d{2})")


def _key(kind, value):
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


def write_daily_files(con, cfg, day):
    """Write output/emails_<day>.csv and output/to_reveal_<day>.csv: only what is new on this
    quota day, never anything already sent on an earlier day. Delivery is tracked as one-way
    hashes in `delivered` (kept forever, like `exported`), so nothing repeats across days.
    Returns (emails, to_reveal) counts for the day."""
    con.execute("CREATE TABLE IF NOT EXISTS delivered(key TEXT PRIMARY KEY, kind TEXT, day TEXT)")
    leads = [(cid, f, json.loads(d)) for cid, f, d in
             con.execute("SELECT channel_id, found_at, data FROM leads ORDER BY found_at")]
    waiting = [(cid, f, json.loads(d)) for cid, f, d in con.execute(
        "SELECT channel_id, found_at, data FROM manual WHERE channel_id NOT IN "
        "(SELECT channel_id FROM manual_done) ORDER BY found_at")]
    if cl.meta_get(con, "delivered_baseline") is None:
        for cid, f, row in leads:
            if f <= DELIVERED_BASELINE_AT:
                con.execute("INSERT OR IGNORE INTO delivered VALUES(?, 'email', 'baseline')", (_key("email", cid),))
                con.execute("INSERT OR IGNORE INTO delivered VALUES(?, 'address', 'baseline')",
                            (_key("address", row["email"].strip().lower()),))
        for cid, f, row in waiting:
            if f <= DELIVERED_BASELINE_AT:
                con.execute("INSERT OR IGNORE INTO delivered VALUES(?, 'reveal', 'baseline')", (_key("reveal", cid),))
        cl.meta_set(con, "delivered_baseline", DELIVERED_BASELINE_AT)

    def day_of(key):
        r = con.execute("SELECT day FROM delivered WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    sup_emails, sup_domains = cl.load_suppression(cfg["suppression_file"])
    emails, lead_channels, lead_urls = [], set(), set()
    for cid, _, row in leads:
        lead_channels.add(cid)
        lead_urls.add((row.get("channel_url") or "").strip().lower())
        k = _key("email", cid)
        d = day_of(k)
        if d is None:
            addr = (row.get("email") or "").strip().lower()
            ok = (cl.is_usable_email(addr) and addr not in sup_emails
                  and addr.split("@")[-1] not in sup_domains)
            if not ok or day_of(_key("address", addr)) is not None:
                d = "skipped"                  # unusable, suppressed, or this address was already sent
            else:
                d = day
                con.execute("INSERT OR IGNORE INTO delivered VALUES(?, 'address', ?)", (_key("address", addr), day))
            con.execute("INSERT OR IGNORE INTO delivered VALUES(?, 'email', ?)", (k, d))
        if d == day:
            emails.append(row)

    reveal, seen_urls = [], set()
    for cid, _, row in waiting:
        url = (row.get("channel_url") or "").strip().lower()
        k = _key("reveal", cid)
        d = day_of(k)
        if d is None:
            d = "skipped" if (cid in lead_channels or url in lead_urls or url in seen_urls) else day
            con.execute("INSERT OR IGNORE INTO delivered VALUES(?, 'reveal', ?)", (k, d))
        if d == day:
            seen_urls.add(url)
            row["channel_id"] = cid
            reveal.append(row)
    con.commit()

    out_dir = cfg["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    cl.write_csv(os.path.join(out_dir, f"emails_{day}.csv"), emails)
    cl.write_csv(os.path.join(out_dir, f"to_reveal_{day}.csv"), cl.sort_manual(reveal))
    # dated files older than the retention window go too (YouTube API 30-day rule)
    oldest = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=cfg["retention_days"])).strftime("%Y-%m-%d")
    for name in os.listdir(out_dir):
        m = DATED_OUTPUTS.match(name)
        if m and m.group(2) < oldest:
            os.remove(os.path.join(out_dir, name))
    return len(emails), len(reveal)


def previous_total(day):
    """Total at the end of the latest progress row before this quota day."""
    if not os.path.exists(PROGRESS):
        return None
    with open(PROGRESS, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["quota_day"] < day]
    return int(rows[-1]["channels_checked_total"]) if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, help="unique emails to find in total (default: target_emails in config.json)")
    ap.add_argument("--status", action="store_true", help="print progress and exit")
    args = ap.parse_args()

    cfg = cl.load_config(os.path.join(HERE, "config.json"))
    if args.target is None:
        args.target = int(cfg.get("target_emails", 100_000))
    day = cl.youtube_quota_day()
    if not os.path.exists(cfg["state_file"]) and not os.path.exists(cfg["db_path"]):
        print("SETUP ERROR: no state/leads.db.gz and no leads.db")
        return 2
    how = cl.sync_from_state(cfg)
    con = cl.open_db(cfg["db_path"])
    before = stats(con, day)
    con.close()
    print(f"Quota day {day} | database {how} | {before['emails_total']:,} emails found so far "
          f"(target {args.target:,}) | {before['leads_total']:,} leads in the 30-day window | "
          f"{before['checked_total']:,} channels checked | {before['pending']:,} queued")
    if args.status:
        return 0
    if before["emails_total"] >= args.target:
        print(f"TARGET REACHED: {before['emails_total']:,} emails found.")
        return 3
    if before["units_used"] >= cfg["daily_unit_budget"] - 20:
        print("ALREADY RAN THIS QUOTA DAY: today's units are spent. Next reset is midnight Pacific time.")
        return 0
    if not (cfg.get("api_key") or os.environ.get("YT_API_KEY")):
        print("SETUP ERROR: set YT_API_KEY")
        return 2

    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    log_path = os.path.join(HERE, "logs", f"{day}.log")
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n===== run started {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC =====\n")
        log.flush()
        rc = subprocess.run([sys.executable, "-u", os.path.join(HERE, "creator_leads.py")],
                            cwd=HERE, stdout=log, stderr=subprocess.STDOUT).returncode
    text = open(log_path, encoding="utf-8").read()
    run_text = text.rsplit("===== run started", 1)[-1]
    crashed = "Traceback (most recent call last)" in run_text
    api_error = "API error:" in run_text
    m = re.findall(r"(\d+) of (\d+) channels in the results were new \((\d+)%\)", text)
    new_pct = m[-1][2] if m else ""

    con = cl.open_db(cfg["db_path"])
    after = stats(con, day)
    new_leads = sum(1 for _ in con.execute("SELECT 1 FROM leads WHERE found_at >= ?", (day,)))
    day_emails, day_reveal = write_daily_files(con, cfg, day)
    con.close()
    prev = previous_total(day)
    checked_today = after["checked_total"] - (prev if prev is not None else before["checked_total"])
    status = "crashed" if crashed or rc != 0 else ("api_error" if api_error else "ok")
    if after["emails_total"] >= args.target:
        status = "target_reached"
    row = {"quota_day": day, "emails_total": after["emails_total"],
           "channels_checked_total": after["checked_total"],
           "channels_checked_today": checked_today, "leads_total": after["leads_total"],
           "new_leads_today": new_leads, "manual_waiting": after["manual_waiting"],
           "searches_used": after["searches_used"], "units_used": after["units_used"],
           "search_results_new_pct": new_pct, "status": status}
    write_progress(row)
    cl.save_to_state(cfg)
    summary = dict(row, pending=after["pending"], target=args.target,
                   emails_file=f"output/emails_{day}.csv ({day_emails})",
                   to_reveal_file=f"output/to_reveal_{day}.csv ({day_reveal})",
                   log=os.path.relpath(log_path, HERE),
                   finished_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print("SUMMARY " + json.dumps(summary))
    if status == "target_reached":
        return 3
    return 1 if status in ("crashed", "api_error") else 0


if __name__ == "__main__":
    sys.exit(main())
