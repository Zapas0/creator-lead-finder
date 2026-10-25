# Creator Lead Finder: instructions for agents

Read this whole file before doing anything in this repo. It is the single source of
instructions. Live numbers are never written here: get them from the commands below.

## What this is

The owner of this copy does cold-email outreach to YouTube creators.
This repo finds YouTube creators who fit (US, Canada, UK, Europe; 1K-250K subscribers;
2+ long-form uploads in the last 30 days; in the niches in `config.json`) and collects the
business email they publish. Everything goes through the **official YouTube Data API v3**
with **one API key**, which the owner gives you. The goal is `target_emails` (config.json)
unique emails found in total, counted from the `exported` table (kept forever as hashes;
`all_leads.csv` only holds the last 30 days, so it is never the total). Runs happen
**only when the owner asks**: there is no schedule.

## Get the current state (always do this first)

```
python3 pilot.py status
```
Shows emails found so far vs the target, leads in the 30-day window, channels checked,
the manual-reveal list, the queue, quota used in the current YouTube quota day, the run lock, and the last runs from
`output/progress.csv`. No API calls.

## Run today's batch (when the owner asks for a run)

```
YT_API_KEY=<key from the owner> nohup python3 pilot.py run > logs/pilot_stdout.txt 2>&1 &
```
`pilot.py run` pulls, takes the run lock, runs `daily.py`, commits and pushes the results
and releases the lock. A run takes 40-70 minutes. Poll every 5-10 minutes:
```
tail -3 logs/$(ls logs | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.log$' | tail -1)
cat logs/pilot_stdout.txt
```
It is finished when `logs/pilot_stdout.txt` ends with `Results pushed and lock released`
(or one of the outcomes below) and no `pilot.py` process is running.

Outcomes (last lines of `logs/pilot_stdout.txt`):
- `SUMMARY {... "status": "ok"}`: done. Do the quality check, then report to the owner.
- `"status": "crashed"` or `"api_error"`: see Troubleshooting; fix; run again. Quota use is
  saved continuously, so a second run the same quota day only spends what's left.
- `ALREADY RAN THIS QUOTA DAY`: today's quota is spent. The quota resets at **midnight
  Pacific time**; tell the owner when, in their time zone.
- `TARGET REACHED`: the goal is met; report final totals and don't run again.
- `ANOTHER RUN IN PROGRESS`: another agent holds the lock. Don't run; check later.
- `SETUP ERROR` / `GIT ERROR`: fix the setup (clean working tree, key, git access) and retry.

**Never run `creator_leads.py` or `daily.py` directly for a real run**: only `pilot.py run`
saves the database to `state/` and handles the lock.

### Quality check after a run
```
python3 - <<'EOF'
import sqlite3, json, collections, creator_leads as cl
con = sqlite3.connect("leads.db")
L = [json.loads(d) for (d,) in con.execute("select data from leads")]
print("leads:", len(L))
print("unusable:", [l["email"] for l in L if not cl.is_usable_email(l["email"])])
print("duplicates:", [e for e, n in collections.Counter(l["email"] for l in L).items() if n > 1])
print("by country:", collections.Counter(l["country"] or "-" for l in L).most_common(8))
print("by niche:", collections.Counter(l["niche"] or "-" for l in L).most_common(8))
EOF
```
If something is wrong, fix the cause in `creator_leads.py` (a filter or extraction rule),
never by hand-editing `leads.db` or the CSVs.

### Report to the owner
Short and plain: emails found in total vs target, new emails this run, leads in the 30-day
window, channels checked (total and this run), manual-reveal list size, search freshness
(`search_results_new_pct`), problems and fixes.
Send him the run's two daily files (the run writes them; the SUMMARY line names them
with their row counts):
- `output/emails_<quota day>.csv`: only the emails new that quota day.
- `output/to_reveal_<quota day>.csv`: only the channels added to the manual-reveal list that
  quota day.
Never send the totals (`all_leads.csv`, `all_manual_check.csv`) unless the owner asks for them.
Before sending, check the two files: no empty or unusable emails, no email or channel
repeated inside a file, and no channel in both files. Any duplicate is a bug in
`write_daily_files` (daily.py): fix it there, never by editing the files.

## Working in parallel with other agents

Several agents may work on this repo at the same time. The rules that keep that safe:

1. **Only one run at a time, enforced by `run.lock`.** `pilot.py run` takes the lock by
   committing and pushing `run.lock`; if another agent pushed first, it backs off.
   A lock expires after 3 hours. `pilot.py release` removes a lock you hold or an expired one.
   Never delete or edit `run.lock` by hand while it's active.
2. **`state/`, `output/` and `logs/` belong to the lock holder.** Only `pilot.py run`
   commits them. Never edit or commit them yourself. `state/leads.db.gz` is binary and
   can't be merged: a conflicting edit would lose a whole day of work.
3. **`leads.db` is a working copy** (gitignored). It is re-extracted automatically whenever
   `state/leads.db.gz` changes, so after a `git pull` you always work on the latest data.
4. **Code and config changes** (creator_leads.py, daily.py, pilot.py, config.json, docs):
   pull first (`git pull --rebase`), make a focused change, test it (see Testing), commit
   with a clear message, `git pull --rebase`, push. Keep database changes backward compatible
   (`CREATE TABLE IF NOT EXISTS`, new columns with defaults): a run may be in progress
   with the previous code.
5. **`reveals.csv` and `suppress.txt` are the owner's.** Append-only for reveals; never remove
   their lines when resolving conflicts.

## Rules (never break these)

- **One API key, one Google Cloud project.** YouTube's developer policies allow one API
  project per app. Never add, rotate or look for other keys, and never create projects to
  get more quota. If the key fails, stop and tell the owner.
- **Official API only.** No scraping of YouTube pages, no yt-dlp or internal YouTube
  endpoints, no automating the "View email address" button, no CAPTCHA solving.
- **Keep the quota budget** in config.json (`daily_search_budget`, `daily_unit_budget`).
  They leave a margin below YouTube's free daily limits. Don't raise them.
- **Don't change targeting without the owner asking**: countries, subscriber range, activity
  filter, niches chosen, confidence rules, email-rules column. Allowed without asking: bug
  fixes, robustness fixes, and adding search terms inside existing niches when freshness drops.
- **Never write the API key into a file, commit, log or message.** Pass it only as the
  `YT_API_KEY` environment variable.
- **Keep the 30-day deletion** of stored API data (`retention_days`, YouTube API policy).
- Creator emails are personal data: don't publish the CSVs or the database anywhere public.

## How the finder works (for debugging)

- **Quota:** each YouTube quota day has a search bucket and a unit bucket, tracked per
  quota day in the database (`meta` table, `searches_<day>` / `units_<day>`). Searches find
  channels; units check them (channel details, video descriptions) and find more channels
  without search (featured channels, public subscriptions, commenters of qualified creators).
- **Search freshness:** every search term keeps a time bookmark (`meta.term_stats`): each
  search asks for videos older than the oldest one it saw last time, so results are never
  repeated. Never-searched terms go first.
- **No repeats:** a channel is checked once (`checked` table); an exported email is never
  exported again (`exported` table keeps hashes). The daily files never repeat what an earlier
  day's file sent (`delivered` table keeps hashes). Dated files in `output/` older than 30 days are deleted by the run.
- **Pipeline per channel:** filters (subscribers, videos, country, niche, activity) ->
  emails from the channel description and recent video descriptions -> if none, up to 200
  older videos -> the creator's own website (linked, or found from links repeated in their
  videos) and its contact pages -> lead (high/medium confidence) or manual-reveal list.
  Sponsor and unrelated-company emails never become leads.
- **Interrupted runs:** channels left half-done are re-queued at the next start.
- **Manual reveals:** `python3 creator_leads.py --review` (the owner, on their computer) opens a
  local page for YouTube's "View email address" button; decisions go to `reveals.csv`,
  which every run merges into the database.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `YouTube says today's search quota is used up` at the start | Searches were used elsewhere this quota day. The run continues with the queue. Mention it. |
| `YouTube says today's unit quota is used up` | Normal end of the day's quota. |
| `API key not valid`, `keyInvalid`, `accessNotConfigured` | Key changed or restricted. Stop and tell the owner. |
| Run ends with no SUMMARY and no Traceback | Process was killed. Run `pilot.py run` again (if your lock is still there it's yours; otherwise it re-takes it). |
| `Traceback` in the log | Smallest fix in the code, test it, push it, run again. |
| `search_results_new_pct` below ~30% for 2+ runs | Add new search terms to existing niches in config.json. |
| Lock held by an agent that no longer exists | Wait for it to expire (3 h), then `pilot.py release`. |
| `GIT ERROR ... could not rebase` | Someone edited files they shouldn't have. Keep the lock holder's `state/`, `output/`, `logs/`; keep the owner's `reveals.csv` lines. |
| Websites timing out a lot | Lower `website_workers` or `website_timeout_seconds` in config.json. |

## Testing a code change

No test suite runs against YouTube. Test changed functions directly with small fake
inputs, e.g. replace `creator_leads.YouTubeAPI._request` with a function returning canned
API responses, point `db_path` and `state_file` at a temporary folder, and run
`creator_leads.main(["--config", "<temp config>", "--max-searches", "1"])`. Never test with
the real key against the real database outside `pilot.py run`.

## Files

| Path | What it is |
|---|---|
| `AGENTS.md` | These instructions (CLAUDE.md imports it). |
| `README.md` | Short overview and setup for the owner. |
| `pilot.py` | Entry point: `status`, `run`, `release`. |
| `daily.py` | One run: sync database, run the finder, save state, progress row, summary. |
| `creator_leads.py` | The finder (standard library only). `--list-niches`, `--review`. |
| `config.json` | Niches and search terms, countries, filters, quota budget, target. No key. |
| `state/leads.db.gz` | The shared database. Written only by `pilot.py run`. |
| `output/emails_<day>.csv` | Emails new that quota day (what the owner gets after each run). |
| `output/to_reveal_<day>.csv` | Channels added to the manual-reveal list that quota day. |
| `output/all_leads.csv` | Every lead in the 30-day window. |
| `output/all_manual_check.csv` | Good-fit creators without a public email. |
| `output/progress.csv` | One row per quota day (`emails_total` is the goal metric). |
| `logs/<quota day>.log` | Full log of that day's runs. |
| `reveals.csv` | The owner's manual reveals (merged on every run). |
| `suppress.txt` | Emails or @domains never to output. |
