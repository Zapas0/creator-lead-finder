# Creator Lead Finder

Finds YouTube creators (US, Canada, UK, Europe; 1K-250K subscribers; 2+ long-form uploads
in the last 30 days; in the niches in `config.json`) who publish a business email, for
cold-email outreach. Uses the official YouTube Data API v3 and its free daily quota.
Agents: read `AGENTS.md`.

## Set up your own copy (once)

1. **Get your own API key.** In Google Cloud Console: create a project, enable
   "YouTube Data API v3", then Credentials > Create credentials > API key.
   Use one key from one project only (YouTube's developer policies allow one project per app).
2. **Put this folder in a private GitHub repo** (private: it will hold creator emails).
   Create an empty private repo on GitHub, then in this folder:
   ```
   git init -b main
   git add .
   git commit -m "Initial copy"
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. **Adjust the targeting if you want** in `config.json`: `countries`, `min_subscribers`,
   `max_subscribers`, `min_long_uploads_30d`, `active_niches` and the niches' search terms
   (`python3 creator_leads.py --list-niches` lists them). `target_emails` is the goal.
4. **Run it.** Give an agent (for example Claude) the repo and your key and ask for a run;
   it follows `AGENTS.md`. Or run it yourself (Python 3.9+, no packages needed):
   ```
   YT_API_KEY=<your key> python3 pilot.py run
   ```
   One run a day uses the day's free quota (it resets at midnight Pacific time) and takes
   40-70 minutes. `python3 pilot.py status` shows where things stand.

## Results (after each run)

- `output/emails_<date>.csv`: creators with an email found that day (new ones only).
- `output/to_reveal_<date>.csv`: good-fit creators added that day whose email you reveal by hand.
- `output/all_leads.csv` / `output/all_manual_check.csv`: everything in the last 30 days.
- `output/progress.csv`: one row per day.

Stored YouTube data is deleted after 30 days (YouTube API policy), and creator emails are
personal data: keep the repo private and don't publish the CSVs.

## Revealing emails by hand (optional, on your computer)

```
git pull
python3 creator_leads.py --review
git add reveals.csv && git commit -m "reveals" && git push
```
The page opens in your browser (stay logged in to YouTube). Your reveals go to
`reveals.csv`, and the next run merges them. Don't commit anything else, and don't start a
real run on your computer while an agent also runs this repo: the quota is shared.
