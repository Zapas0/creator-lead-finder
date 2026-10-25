#!/usr/bin/env python3
"""
creator_leads.py - find YouTube creators likely to buy thumbnails and collect
the business emails they post publicly.

Uses only the official YouTube Data API v3 (free quota) plus one polite visit to
a creator's own website. It does not scrape YouTube pages, solve CAPTCHAs, or
touch Linktree / Instagram / TikTok. Standard library only - no pip installs.

Quick start
  1. Free API key: console.cloud.google.com > create ONE project > enable
     "YouTube Data API v3" > Credentials > Create credentials > API key.
  2. Put the key in config.json ("api_key") or set the YT_API_KEY env variable.
  3. python3 creator_leads.py

Every run writes to ./output/:
  leads_<time>.csv         new creators with a public email (one row per channel)
  manual_check_<time>.csv  good-fit creators with no public email: open the
                           channel's About tab and click "View email address"
  all_leads.csv            every lead still inside the 30-day retention window
  all_manual_check.csv     same, for the manual list

Run it yourself once a day (--niche NAME to pick niches). It uses up to 98 of the
100 free searches and 9,800 of the 10,000 units, which reset at midnight Pacific time.
State lives in leads.db, so channels are never checked twice and the keyword
rotation continues where the last run stopped.

Before a channel lands on the manual list, the script digs deeper on its own:
it reads up to 200 older video descriptions, spots the creator's own website
from links repeated across their videos, and checks its contact/about pages.

  python3 creator_leads.py --review
opens a local page that walks you through the manual list: it opens each About
page in one browser tab, you click "View email address" and paste the email,
and it is saved straight into your leads files. About 10 seconds per channel.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

API_BASE = "https://www.googleapis.com/youtube/v3/"

DEFAULT_CONFIG = {
    "api_key": "",
    # Pick which niches to search: "all", or a list like ["gaming", "finance"].
    # Override for one run with:  python3 creator_leads.py --niche gaming --niche tech
    "active_niches": "all",
    # Each niche: "search" = YouTube search terms, "topics" = YouTube's own topic labels,
    # "words" = words in a channel's name/description. Topics and words decide the niche
    # of channels found without search (featured / followed / commenters); channels that
    # match none of your active niches are skipped. Add or edit niches freely.
    "niches": {
        "gaming": {
            "search": ["minecraft", "roblox", "fortnite", "gaming commentary", "indie game review",
                       "call of duty", "valorant", "gta", "pokemon", "horror game"],
            "topics": ["Video_game_culture", "Action_game", "Action-adventure_game", "Casual_game",
                       "Puzzle_video_game", "Racing_video_game", "Role-playing_video_game",
                       "Simulation_video_game", "Sports_game", "Strategy_video_game", "Music_video_game"],
            "words": ["gaming", "gameplay", "let's play", "minecraft", "roblox", "fortnite", "gamer"]},
        "finance": {
            "search": ["personal finance", "investing for beginners", "stock market", "real estate investing",
                       "side hustle", "make money online", "small business tips", "crypto"],
            "topics": ["Business"],
            "words": ["finance", "investing", "money", "stocks", "real estate", "business", "crypto", "wealth"]},
        "tech": {
            "search": ["tech review", "smartphone review", "pc build", "ai tools", "productivity apps"],
            "topics": ["Technology"],
            "words": ["tech", "technology", "gadget", "pc build", "software", "ai tools"]},
        "true_crime_mystery": {
            "search": ["true crime", "horror stories", "unsolved mysteries", "paranormal", "scary stories"],
            "topics": [],
            "words": ["true crime", "murder", "mystery", "unsolved", "paranormal", "horror", "scary", "creepy"]},
        "education": {
            "search": ["history documentary", "science explained", "language learning", "geography facts", "psychology"],
            "topics": ["Knowledge"],
            "words": ["history", "science", "documentary", "explained", "learn", "education", "facts"]},
        "self_improvement": {
            "search": ["self improvement", "productivity tips", "motivation", "study with me", "minimalism"],
            "topics": [],
            "words": ["self improvement", "productivity", "motivation", "mindset", "habits", "discipline"]},
        "fitness": {
            "search": ["fitness workout", "bodybuilding", "weight loss journey", "calisthenics", "running"],
            "topics": ["Physical_fitness", "Health"],
            "words": ["fitness", "workout", "gym", "bodybuilding", "training", "calisthenics", "running"]},
        "food": {
            "search": ["cooking recipes", "easy dinner recipes", "baking", "bbq", "food review"],
            "topics": ["Food"],
            "words": ["cooking", "recipe", "recipes", "kitchen", "baking", "bbq", "chef", "food"]},
        "travel": {
            "search": ["budget travel", "travel vlog", "van life", "rv living", "solo travel"],
            "topics": ["Tourism"],
            "words": ["travel", "van life", "vanlife", "rv", "nomad", "adventure", "trip"]},
        "outdoors": {
            "search": ["camping", "fishing", "hunting", "hiking", "bushcraft"],
            "topics": [],
            "words": ["camping", "fishing", "hunting", "hiking", "bushcraft", "outdoors", "survival", "overlanding"]},
        "cars": {
            "search": ["car review", "car restoration", "car detailing", "motorcycle vlog", "project car"],
            "topics": ["Vehicle", "Motorsport"],
            "words": ["car", "cars", "auto", "garage", "restoration", "motorcycle", "truck", "detailing"]},
        "diy_home": {
            "search": ["diy home renovation", "woodworking", "gardening", "homesteading", "tiny house"],
            "topics": [],
            "words": ["diy", "woodworking", "renovation", "garden", "gardening", "homestead", "build", "tiny house"]},
        "creative": {
            "search": ["drawing tutorial", "music production", "photography tips", "animation", "video editing tutorial"],
            "topics": ["Music"],
            "words": ["art", "drawing", "painting", "music production", "photography", "animation", "editing"]},
        "entertainment": {
            "search": ["movie review", "anime review", "commentary drama", "reaction video", "comedy sketch"],
            "topics": ["Film", "Television_program", "Humour", "Entertainment"],
            "words": ["movie", "film", "anime", "commentary", "reaction", "comedy", "review"]},
        "sports": {
            "search": ["sports analysis", "nba analysis", "nfl breakdown", "soccer analysis", "golf tips"],
            "topics": ["Sport", "Association_football", "American_football", "Basketball", "Baseball",
                       "Boxing", "Golf", "Ice_hockey", "Mixed_martial_arts", "Tennis", "Professional_wrestling"],
            "words": ["sports", "nba", "nfl", "soccer", "football", "basketball", "golf", "mma", "boxing"]},
        "podcasts": {
            "search": ["podcast", "interview podcast", "comedy podcast"],
            "topics": [],
            "words": ["podcast", "episode", "interview"]},
        "beauty_fashion": {
            "search": ["makeup tutorial", "skincare routine", "fashion haul", "hair tutorial"],
            "topics": ["Fashion", "Physical_attractiveness"],
            "words": ["makeup", "skincare", "beauty", "fashion", "hair", "outfit"]},
        "pets": {
            "search": ["dog training", "cat vlog", "aquarium", "reptiles"],
            "topics": ["Pet"],
            "words": ["dog", "dogs", "cat", "cats", "puppy", "pets", "aquarium", "reptile"]},
    },
    "video_durations": ["medium", "long"],    # medium = 4-20 min, long = 20+ min
    "max_pages_per_query": 6,                  # max searches per term per run (few niches -> deeper)
    # YouTube's free quota per day: 100 searches + 10,000 units. These budgets leave a
    # small safety margin and are tracked per quota day, so a second run the same day
    # only spends what is left.
    "daily_search_budget": 98,
    "daily_unit_budget": 9800,
    # Each search term keeps a bookmark in time: every search continues from the oldest
    # video the previous one returned and goes further back, so the same videos are never
    # returned twice. A term walks back this many days, then starts again from today,
    # covering only uploads newer than its last walk.
    "search_lookback_days": 60,
    "resweep_after_hours": 24,
    "relevance_language": "en",
    "region_code": "",                         # "" = no country bias (finds UK/Europe too)
    "min_subscribers": 1000,
    "max_subscribers": 250000,
    "min_total_videos": 20,
    # Channel country filter: US, Canada, UK and Europe (EU + EEA + Switzerland).
    # The "email_rules" column in the CSV says which law applies to each lead.
    "countries": ["US", "CA", "GB", "IE",
                  "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
                  "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
                  "NO", "IS", "LI", "CH"],
    "allow_unknown_country": True,             # many US creators leave country blank
    "min_long_uploads_30d": 2,
    "long_form_min_seconds": 181,              # Shorts can run up to 3 minutes
    "videos_to_scan": 50,
    "deep_scan_videos": 200,                   # before giving up on a channel, read this many uploads (0 = off)
    "guess_site_from_videos": True,            # find the creator's site from links repeated in their videos
    "max_site_pages": 5,                       # pages visited per website (home, contact, about...)
    "daily_reveal_target": 15,                 # review page: YouTube caps reveals at roughly 10-20/day/account
    "review_port": 8765,
    # After the 100 searches, spend the rest of the daily units finding channels without
    # search: the featured channels, public subscriptions and commenters of creators that
    # already qualified (about 3 units per creator, up to ~150 new channels each).
    "snowball": True,
    "snowball_sources_per_round": 30,
    "fetch_websites": True,
    "website_workers": 8,
    "website_timeout_seconds": 10,
    "user_agent": "Mozilla/5.0 (compatible; CreatorLeadFinder/1.0)",
    "retention_days": 30,                      # YouTube API policy: refresh or delete after 30 days
    "output_dir": "output",
    "db_path": "leads.db",                     # working copy; never committed
    "state_file": "state/leads.db.gz",         # the shared database, committed to the repo
    "target_emails": 100000,                   # stop once this many unique emails have been found in total
    "suppression_file": "suppress.txt",
    "reveals_file": "reveals.csv",             # every reveal decision, merged into the database on each run
    "skip_link_domains": [
        "youtube.com", "youtu.be", "google.com", "goo.gl", "instagram.com",
        "tiktok.com", "twitter.com", "x.com", "facebook.com", "fb.com", "fb.me",
        "threads.net", "snapchat.com", "reddit.com", "discord.gg", "discord.com",
        "twitch.tv", "kick.com", "patreon.com", "ko-fi.com", "buymeacoffee.com",
        "paypal.com", "paypal.me", "venmo.com", "cash.app", "linktr.ee",
        "linktree.com", "beacons.ai", "stan.store", "bio.link", "lnk.bio",
        "campsite.bio", "msha.ke", "amazon.com", "amzn.to", "a.co", "bit.ly",
        "tinyurl.com", "ow.ly", "t.co", "geni.us", "spotify.com", "apple.com",
        "soundcloud.com", "linkedin.com", "pinterest.com", "tumblr.com",
        "medium.com", "substack.com", "gofundme.com", "teespring.com",
        "creator-spring.com", "fourthwall.com", "streamlabs.com",
        "streamelements.com", "epidemicsound.com", "skillshare.com",
        "squarespace.com", "audible.com", "wa.me", "t.me", "telegram.me",
        "gmail.com", "nordvpn.com", "expressvpn.com",
    ],
}

# --------------------------------------------------------------------------- #
# Email extraction
# --------------------------------------------------------------------------- #

EMAIL_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}"
)
BRACKET_AT = re.compile(r"\s*[\[\(\{<]\s*(?:at|@)\s*[\]\)\}>]\s*", re.I)
BRACKET_DOT = re.compile(r"\s*[\[\(\{<]\s*(?:dot|\.)\s*[\]\)\}>]\s*", re.I)
WORD_FORM = re.compile(
    r"\b([A-Za-z0-9._%+-]+)\s+at\s+([A-Za-z0-9-]+)\s+dot\s+([A-Za-z]{2,24})\b", re.I
)
SPACED_AT = re.compile(r"([A-Za-z0-9._%+-])\s+@\s+([A-Za-z0-9-])")
CF_ATTR = re.compile(r'data-cfemail="([0-9a-fA-F]{4,})"')
CF_HREF = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]{4,})")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'(){}\[\]]+", re.I)
BUSINESS_CTX = re.compile(
    r"business|inquir|enquir|contact|collab|sponsorships?\b|partnerships?|booking|"
    r"management|brand deals?|work with (?:me|us)|e-?mail|reach (?:me|us)", re.I)
# Text right before an email that means it belongs to a sponsor or a shop, not the creator.
SPONSOR_CTX = re.compile(
    r"sponsored by|sponsoring|sponsor of|today'?s sponsor|thanks to .{0,40} for|promo|discount|"
    r"coupon|% off|use code|affiliate|free trial|customer (?:support|service)|"
    r"sign up|download (?:the|it)|get \d+", re.I)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#\s]+)["']""", re.I)
CONTACT_LINK = re.compile(
    r"contact|about|business|work-?with|collab|sponsor|partner|press|media-?kit|booking|hire", re.I)
FALLBACK_PATHS = ("/contact", "/contact-us", "/about", "/work-with-me")
GENERIC_TITLE_WORDS = {
    "gaming", "games", "game", "plays", "play", "official", "channel", "videos", "video",
    "with", "the", "and", "show", "podcast", "studio", "studios", "media", "music", "news",
    "daily", "life", "world", "review", "reviews", "tech", "cooking", "kitchen", "fitness",
    "travel", "outdoors", "fishing", "hunting", "cars", "auto", "home", "build", "builds",
    "diy", "art", "films", "productions", "academy", "school", "learn", "tips", "guide",
}
ABOUT_EMAIL_HINT = re.compile(
    r"about (?:tab|page|section)|view email|business email|email (?:is )?(?:on|in) (?:my|the) about",
    re.I)

FILE_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "mp4", "mov", "pdf", "ico"}
JUNK_DOMAINS = {
    "example.com", "domain.com", "email.com", "yourdomain.com", "yoursite.com",
    "mysite.com", "test.com", "sentry.io", "wixpress.com", "sentry.wixpress.com",
    "sentry-next.wixpress.com", "godaddy.com", "squarespace.com", "shopify.com",
    "company.com", "yourcompany.com", "website.com", "yourwebsite.com", "mail.example",
}
JUNK_DOMAIN_SUFFIXES = (".sentry.io", ".wixpress.com", ".ingest.sentry.io", ".sentry-cdn.com")
# 2-letter country TLDs are always accepted; longer TLDs must be on this list, which
# filters out text like "fishing@red.pier" that is not a real address.
KNOWN_GTLDS = set("""
com net org edu gov mil int info biz name pro xyz online site store shop tech app dev
art studio studios media live life club blog news agency digital design world space today
email page website network solutions company group team global academy marketing services
consulting games game fun video photo photography film films music audio band events fitness
coach health family travel tours house homes realty estate properties money finance capital
fund investments bio eco green earth farm garden kitchen cafe food recipes pet dog cat cars
auto bike golf fish surf camp ski run yoga beauty hair fashion style clothing boutique gallery
gifts toys wine beer pizza center school university courses education training tips guide
reviews press social chat link click one plus zone works tools systems software cloud host
hosting wiki land pub bar city town place ninja rocks guru expert legal law tax church love
gay lgbt mom kids baby care clinic dental doctor fit gym diet bet poker casino win cool
productions production creative graphics photos pics pictures show tv watch stream radio
podcast rocks lol farm ranch outdoors fishing hunting boats yachts cruises vacations
holiday flights hotel rentals apartments construction builders contractors repair plumbing
energy solar engineering industries supply parts tires garage motorcycles quest
""".split())
GENERIC_CCTLDS = {"co", "io", "tv", "me", "gg", "ai", "fm", "ly", "cc", "so", "to", "ws", "la", "sh", "gl", "vc", "ac"}
JUNK_LOCAL_PREFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon")
JUNK_LOCAL_EXACT = {
    "postmaster", "abuse", "privacy", "dmca", "copyright", "legal", "unsubscribe",
    "name", "email", "user", "yourname", "youremail", "username", "someone", "you",
    "john.doe", "johndoe", "jane.doe", "janedoe", "example", "test",
}
FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.ca",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com",
    "mac.com", "aol.com", "protonmail.com", "proton.me", "pm.me", "gmx.com",
    "gmx.net", "mail.com", "zoho.com", "yandex.com", "hey.com",
}


def _fix_tld(raw):
    """Cut a TLD glued to the next word, e.g. 'me@x.comFollow' -> 'me@x.com'."""
    head, _, tld = raw.rpartition(".")
    if tld[:1].islower():
        m = re.match(r"[a-z]+", tld)
        if m and m.end() < len(tld) and tld[m.end()].isupper():
            tld = tld[: m.end()]
    return head + "." + tld


def is_usable_email(email):
    if email.count("@") != 1 or ".." in email:
        return False
    local, domain = email.split("@")
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) < 2 or tld in FILE_TLDS or len(domain) < 4:
        return False
    if domain in JUNK_DOMAINS or domain.endswith(JUNK_DOMAIN_SUFFIXES) or domain == "sentry.io":
        return False
    if len(tld) != 2 and tld not in KNOWN_GTLDS:
        return False
    if local in JUNK_LOCAL_EXACT or re.fullmatch(r"[0-9a-f]{20,}", local):
        return False
    return not local.startswith(JUNK_LOCAL_PREFIXES)


def find_emails(text):
    """Return [(email, business_context, sponsor_context)] found in text, de-obfuscated."""
    if not text:
        return []
    t = re.sub(r"\\u00[0-9a-fA-F]{2}|(?<![A-Za-z0-9])u003[ceCE]", " ", text)   # JSON-escaped < >
    t = BRACKET_AT.sub("@", t)
    t = BRACKET_DOT.sub(".", t)
    t = WORD_FORM.sub(lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", t)
    t = SPACED_AT.sub(r"\1@\2", t)
    found = []
    for m in EMAIL_RE.finditer(t):
        email = _fix_tld(m.group(0)).strip("._-").lower()
        if is_usable_email(email):
            context = t[max(0, m.start() - 80): m.start()]
            line = context.rsplit("\n", 1)[-1]          # sponsor talk only counts on the same line
            found.append((email, bool(BUSINESS_CTX.search(context)), bool(SPONSOR_CTX.search(line))))
    return found


def name_matches(email, title):
    """Does a custom-domain email look like it belongs to this channel?"""
    local, domain = email.split("@")
    label = domain.split(".")[-2] if "." in domain else domain
    if domain.count(".") >= 2 and len(domain.split(".")[-2]) <= 3:      # e.g. name.co.uk
        label = domain.split(".")[-3]
    tnorm = re.sub(r"[^a-z0-9]", "", (title or "").lower())
    if len(tnorm) >= 3 and (tnorm in label or (len(label) >= 4 and label in tnorm)):
        return True
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (title or "").lower()) if w not in GENERIC_TITLE_WORDS]
    return any(w in label or w in local for w in words)


EU_COUNTRIES = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
                "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
EEA_OTHER = {"NO", "IS", "LI", "CH"}


def email_rules(country, email=""):
    """One-line reminder of the cold-email rule for this lead's country (not legal advice)."""
    c = (country or "").upper() or {"UK": "GB"}.get(email_country(email) or "", email_country(email) or "")
    if c == "DE":
        return "DE: prior consent needed even B2B - don't cold email"
    if c == "GB":
        return "UK: OK if they trade as a company; sole traders need consent"
    if c in EU_COUNTRIES or c in EEA_OTHER or c == "EU":
        return "EU/GDPR: say where you found the email + easy opt-out"
    if c == "CA":
        return "CASL: keep source_ref as proof + opt-out"
    return "US/CAN-SPAM: postal address + opt-out"


def email_country(email):
    """Country implied by the email's TLD (e.g. .de -> DE, .co.uk -> UK); None for .com, .co, .io..."""
    tld = email.rsplit(".", 1)[-1]
    if len(tld) == 2 and tld not in GENERIC_CCTLDS:
        return tld.upper()
    return None


def cloudflare_emails(html):
    out = []
    for h in CF_ATTR.findall(html) + CF_HREF.findall(html):
        try:
            key = int(h[:2], 16)
            out.append("".join(chr(int(h[i:i + 2], 16) ^ key) for i in range(2, len(h), 2)))
        except ValueError:
            pass
    return out


def host_of(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def own_site_urls(text, skip_domains, limit=2):
    urls, hosts = [], set()
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?*")
        if not url.lower().startswith("http"):
            url = "https://" + url
        host = host_of(url)
        if not host or "." not in host or host in hosts:
            continue
        if any(host == d or host.endswith("." + d) for d in skip_domains):
            continue
        hosts.add(host)
        urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def guess_own_sites(title, descriptions, skip_domains, min_videos=3):
    """A domain linked in 3+ of the creator's videos whose name matches the channel name."""
    tnorm = re.sub(r"[^a-z0-9]", "", (title or "").lower())
    words = [w for w in re.findall(r"[a-z0-9]{4,}", (title or "").lower()) if w not in GENERIC_TITLE_WORDS]
    counts = {}
    for _, desc in descriptions:
        hosts = set()
        for raw in URL_RE.findall(desc or ""):
            url = raw.rstrip(".,;:!?*")
            host = host_of(url if url.lower().startswith("http") else "https://" + url)
            if host and "." in host and not any(host == d or host.endswith("." + d) for d in skip_domains):
                hosts.add(host)
        for h in hosts:
            counts[h] = counts.get(h, 0) + 1
    for host, n in sorted(counts.items(), key=lambda x: -x[1]):
        if n < min_videos:
            break
        label = host.split(".")[-2]
        if len(label) < 4:
            continue
        if (len(tnorm) >= 4 and (label in tnorm or tnorm in label)) or any(w in label for w in words):
            return ["https://" + host]
    return []


# --------------------------------------------------------------------------- #
# YouTube Data API client with quota accounting
# --------------------------------------------------------------------------- #

class QuotaExhausted(Exception):
    pass


class ApiError(Exception):
    pass


class YouTubeAPI:
    def __init__(self, key, max_units, max_searches):
        self.key = key
        self.max_units = max_units
        self.max_searches = max_searches
        self.units = 0
        self.searches = 0
        self.server_exhausted = set()
        self.persist = lambda: None      # main() sets this to save today's usage to the database

    def _request(self, method, params):
        query = dict(params)
        query["key"] = self.key
        url = API_BASE + method + "?" + urllib.parse.urlencode(query)
        last = None
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                reason, message = "", body[:300]
                try:
                    err = json.loads(body)["error"]
                    message = err.get("message", message)
                    reason = (err.get("errors") or [{}])[0].get("reason", "")
                except (ValueError, KeyError, TypeError):
                    pass
                # Daily caps: 403 quotaExceeded/dailyLimitExceeded, or (search bucket) 429 with
                # "Search Queries per day" in the message. Never retry those.
                text = (reason + " " + message).lower()
                if e.code in (403, 429) and ("quota" in text or "dailylimit" in text or "per day" in text):
                    self.server_exhausted.add("search" if method == "search" else "units")
                    raise QuotaExhausted(f"YouTube says today's {'search' if method == 'search' else 'unit'} "
                                         f"quota is used up")
                if e.code == 429 or e.code >= 500 or reason in ("rateLimitExceeded", "userRateLimitExceeded"):
                    last = f"HTTP {e.code} {reason}"
                    time.sleep(2 ** attempt)
                    continue
                if e.code == 404:
                    return {"items": []}
                raise ApiError(f"{method} HTTP {e.code} {reason}: {message}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
                time.sleep(2 ** attempt)
        raise ApiError(f"{method}: failed after retries ({last})")

    def search(self, **params):
        if self.searches >= self.max_searches:
            raise QuotaExhausted("today's search budget is used up")
        self.searches += 1
        return self._request("search", params)

    def call(self, method, **params):
        if self.units >= self.max_units:
            raise QuotaExhausted("today's unit budget is used up")
        self.units += 1
        return self._request(method, params)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS pending(channel_id TEXT PRIMARY KEY, keyword TEXT, added_at TEXT);
        CREATE TABLE IF NOT EXISTS checked(channel_id TEXT PRIMARY KEY, checked_at TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS leads(channel_id TEXT PRIMARY KEY, found_at TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS manual(channel_id TEXT PRIMARY KEY, found_at TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS manual_done(channel_id TEXT PRIMARY KEY, done_at TEXT, outcome TEXT);
        CREATE TABLE IF NOT EXISTS snowball_done(channel_id TEXT PRIMARY KEY, done_at TEXT);
        -- one-way hashes of every email ever exported, kept forever so nobody is emailed twice
        CREATE TABLE IF NOT EXISTS exported(email_hash TEXT PRIMARY KEY, first_exported TEXT);
        """
    )
    return con


def purge_old(con, days):
    cutoff = iso(now_utc() - timedelta(days=days))
    removed = 0
    # keep a running count of checked channels that were purged, for lifetime progress totals
    n_checked = con.execute("SELECT COUNT(*) FROM checked WHERE checked_at < ?", (cutoff,)).fetchone()[0]
    if n_checked:
        prev = con.execute("SELECT value FROM meta WHERE key='purged_checked'").fetchone()
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('purged_checked', ?)",
                    (str(int(prev[0] if prev else 0) + n_checked),))
    for table, col in (("pending", "added_at"), ("checked", "checked_at"),
                       ("leads", "found_at"), ("manual", "found_at"), ("manual_done", "done_at"),
                       ("snowball_done", "done_at")):
        removed += con.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,)).rowcount
    con.commit()
    return removed


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))
    con.commit()


def mark_checked(con, cid, result):
    con.execute("INSERT OR REPLACE INTO checked VALUES(?, ?, ?)", (cid, iso(now_utc()), result))
    con.execute("DELETE FROM pending WHERE channel_id=?", (cid,))


def load_suppression(path):
    emails, domains = set(), set()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                item = line.strip().lower()
                if not item or item.startswith("#"):
                    continue
                if "@" in item and not item.startswith("@"):
                    emails.add(item)
                else:
                    domains.add(item.lstrip("@"))
    return emails, domains


# --------------------------------------------------------------------------- #
# Pipeline steps
# --------------------------------------------------------------------------- #

def discover(api, con, cfg):
    """Spend the search budget on niche search terms x video lengths, newest uploads first.

    Every term keeps a time bookmark, so each search returns videos that term has never
    returned before (today or on earlier days):
      - a walk starts at "now" and moves back in time: each search asks for videos
        published before the oldest one the previous search returned;
      - it ends after search_lookback_days, or when YouTube has nothing older;
      - the next walk (after resweep_after_hours) only covers uploads newer than the top
        of the previous walk.
    Order: never-searched terms first, then walks in progress with the best share of new
    channels last time, then terms ready for a new walk. Terms that finished a walk
    recently are skipped."""
    terms = [t for n in active_niches(cfg) for t in cfg["niches"][n]["search"]]
    pairs = [(kw, d) for kw in terms for d in cfg["video_durations"]]
    if not pairs:
        return 0
    tstats = json.loads(meta_get(con, "term_stats", "{}"))
    now = now_utc()
    today = now.strftime("%Y-%m-%d")
    floor = iso(now - timedelta(days=cfg["search_lookback_days"]))
    resweep = timedelta(hours=cfg["resweep_after_hours"])

    def state(pair):
        st = tstats.setdefault(f"{pair[0]}|{pair[1]}",
                               {"searches": 0, "new": 0, "last_ratio": 1.0, "last_day": ""})
        st.setdefault("before", None)      # walk in progress: continue below this time
        st.setdefault("after", None)       # lower bound of the walk in progress
        st.setdefault("top", None)         # upper bound of the walk in progress
        st.setdefault("last_top", None)    # upper bound of the last finished walk
        st.setdefault("walk_done_at", None)
        return st

    def tier(pair):
        st = state(pair)
        if st["searches"] == 0:
            return (0, 0.0, "")
        if st["before"]:
            return (1, -st["last_ratio"], st["last_day"])
        done = st["walk_done_at"]
        if done and now - parse_time(done) < resweep:
            return (9, 0.0, "")            # finished a walk recently: nothing new yet
        return (2, -st["last_ratio"], st["last_day"])

    pairs.sort(key=tier)
    new_channels, results_total = 0, 0
    try:
        for rnd in range(cfg["max_pages_per_query"]):
            live = [p for p in pairs if tier(p)[0] < 9 and (rnd == 0 or state(p)["before"])]
            if not live:
                if api.searches < api.max_searches:
                    print(f"  Every search term in these niches has already returned all its videos "
                          f"from the last {cfg['search_lookback_days']} days. {api.max_searches - api.searches} "
                          f"searches left unused: add more search terms or niches in config.json.")
                break
            for kw, duration in live:
                st = state((kw, duration))
                if not st["before"]:           # start a new walk from now
                    st["top"] = iso(now_utc())
                    st["after"] = max(st["last_top"] or floor, floor)
                params = dict(part="snippet", type="video", q=kw, maxResults=50,
                              order="date", videoDuration=duration, publishedAfter=st["after"])
                if st["before"]:
                    params["publishedBefore"] = st["before"]
                if cfg.get("relevance_language"):
                    params["relevanceLanguage"] = cfg["relevance_language"]
                if cfg.get("region_code"):
                    params["regionCode"] = cfg["region_code"]
                try:
                    data = api.search(**params)
                except ApiError as e:
                    print(f"  search failed, stopping searches for this run: {e}")
                    raise QuotaExhausted("search error")
                items = data.get("items", [])
                chans = {(it.get("snippet") or {}).get("channelId") for it in items} - {None}
                fresh = sum(1 for c in chans if queue_channel(con, c, kw))
                new_channels += fresh
                results_total += len(chans)
                st["searches"] += 1
                st["new"] += fresh
                st["last_ratio"] = round(fresh / len(chans), 3) if chans else 0.0
                st["last_day"] = today
                times = [(it.get("snippet") or {}).get("publishedAt") for it in items]
                times = [t for t in times if t]
                oldest = min(times) if times else None
                where = f"before {st['before'][:10]}" if st["before"] else "newest"
                if not items or not data.get("nextPageToken") or not oldest or oldest[:19] <= st["after"][:19]:
                    st["last_top"], st["before"], st["walk_done_at"] = st["top"], None, iso(now_utc())
                    note = " | walk finished"
                else:
                    st["before"] = iso(parse_time(oldest) - timedelta(seconds=1))
                    note = ""
                con.commit()
                api.persist()
                print(f"  search {api.searches:>3}: '{kw}' ({duration}, {where}) "
                      f"-> {fresh}/{len(chans)} new | {new_channels} new so far{note}")
    except QuotaExhausted as e:
        print(f"  search stopped: {e}")
    finally:
        meta_set(con, "term_stats", json.dumps(tstats))
    if results_total:
        print(f"  {new_channels} of {results_total} channels in the results were new "
              f"({100 * new_channels // results_total}%)")
    return new_channels


def active_niches(cfg):
    names = list(cfg["niches"])
    act = cfg.get("active_niches", "all")
    if act == "all" or not act:
        return names
    unknown = [n for n in act if n not in cfg["niches"]]
    if unknown:
        raise SystemExit(f"Unknown niche(s): {', '.join(unknown)}. Available: {', '.join(names)}")
    return list(act)


def niche_of(channel, keyword, cfg):
    """Niche of a channel: from the search term that found it, else from YouTube's topic
    labels and the words in its name/description (best active niche, or None)."""
    active = active_niches(cfg)
    for name in active:
        if keyword in cfg["niches"][name]["search"]:
            return name
    sn = channel.get("snippet") or {}
    text = " " + re.sub(r"[^a-z0-9' ]+", " ", (sn.get("title", "") + " " + sn.get("description", "")).lower()) + " "
    topics = {t.rsplit("/", 1)[-1].lower() for t in (channel.get("topicDetails") or {}).get("topicCategories", [])}
    best, best_score = None, 0
    for name in active:
        n = cfg["niches"][name]
        score = 2 * len(topics & {t.lower() for t in n.get("topics", [])})
        score += sum(1 for w in set(n.get("words", []) + n.get("search", [])) if f" {w.lower()} " in text)
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= 2 else None


def queue_channel(con, cid, how):
    if not cid:
        return False
    seen = con.execute(
        "SELECT 1 FROM checked WHERE channel_id=? UNION SELECT 1 FROM pending WHERE channel_id=?",
        (cid, cid)).fetchone()
    if seen:
        return False
    con.execute("INSERT INTO pending VALUES(?, ?, ?)", (cid, how, iso(now_utc())))
    return True


def snowball(api, con, cfg):
    """Find channels without search: the featured channels, public subscriptions and
    commenters of creators that already qualified. Up to 3 units per creator."""
    active = set(active_niches(cfg))
    rows = [r for r in con.execute(
        "SELECT channel_id, data FROM leads WHERE channel_id NOT IN (SELECT channel_id FROM snowball_done) "
        "UNION ALL "
        "SELECT channel_id, data FROM manual WHERE channel_id NOT IN (SELECT channel_id FROM snowball_done)")
        if json.loads(r[1]).get("niche") in active][: cfg["snowball_sources_per_round"]]
    added = 0
    for src, data in rows:
        row = json.loads(data)
        label = (row.get("channel_title") or "")[:40]
        found = []
        try:
            d = api.call("channelSections", part="contentDetails", channelId=src)
            found += [(c, f"featured by {label}") for it in d.get("items", [])
                      for c in (it.get("contentDetails") or {}).get("channels", [])]
        except ApiError:
            pass
        try:
            d = api.call("subscriptions", part="snippet", channelId=src, maxResults=50)
            found += [(((it.get("snippet") or {}).get("resourceId") or {}).get("channelId"), f"followed by {label}")
                      for it in d.get("items", [])]
        except ApiError:
            pass                       # subscriptions are often private
        vid = (row.get("latest_video_url") or "").partition("v=")[2]
        if vid:
            try:
                d = api.call("commentThreads", part="snippet", videoId=vid, maxResults=100, textFormat="plainText")
                for it in d.get("items", []):
                    top = ((it.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
                    found.append(((top.get("authorChannelId") or {}).get("value"), f"commented on {label}"))
            except ApiError:
                pass                   # comments disabled
        for cid, how in found:
            if cid != src and queue_channel(con, cid, how):
                added += 1
        con.execute("INSERT OR REPLACE INTO snowball_done VALUES(?, ?)", (src, iso(now_utc())))
        con.commit()
        api.persist()
    return added, len(rows)


def parse_duration(value):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_time(value):
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def reject_reason(channel, cfg):
    stats = channel.get("statistics") or {}
    if stats.get("hiddenSubscriberCount"):
        return "hidden_subscribers"
    subs = int(stats.get("subscriberCount", 0))
    if subs < cfg["min_subscribers"] or subs > cfg["max_subscribers"]:
        return f"subscribers_out_of_range:{subs}"
    if int(stats.get("videoCount", 0)) < cfg["min_total_videos"]:
        return "too_few_videos"
    country = ((channel.get("snippet") or {}).get("country") or "").upper()
    if country:
        if cfg["countries"] and country not in [c.upper() for c in cfg["countries"]]:
            return f"country_{country}"
    elif not cfg["allow_unknown_country"]:
        return "country_unknown"
    return None


def analyze_videos(api, channel, cfg):
    """Fetch recent uploads; return activity stats and per-video description emails."""
    uploads = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if not uploads:
        return None
    pl = api.call("playlistItems", part="contentDetails", playlistId=uploads,
                  maxResults=min(50, cfg["videos_to_scan"]))
    ids = [it["contentDetails"]["videoId"] for it in pl.get("items", []) if it.get("contentDetails")]
    if not ids:
        return None
    vids = api.call("videos", part="snippet,contentDetails,statistics", id=",".join(ids))
    now = now_utc()
    long_videos, descriptions = [], []
    for v in vids.get("items", []):
        sn = v.get("snippet") or {}
        descriptions.append((v["id"], sn.get("description", "")))
        if sn.get("liveBroadcastContent") in ("live", "upcoming"):
            continue
        dur = parse_duration((v.get("contentDetails") or {}).get("duration"))
        if dur < cfg["long_form_min_seconds"]:
            continue
        long_videos.append({
            "id": v["id"],
            "title": sn.get("title", ""),
            "description": sn.get("description", ""),
            "published": parse_time(sn.get("publishedAt", "1970-01-01T00:00:00")),
            "views": int((v.get("statistics") or {}).get("viewCount", 0)),
        })
    long_videos.sort(key=lambda x: x["published"], reverse=True)
    recent = [v for v in long_videos if v["published"] >= now - timedelta(days=30)]
    settled = [v["views"] for v in long_videos if v["published"] <= now - timedelta(days=3)][:15]
    return {
        "long_uploads_30d": len(recent),
        "median_views": int(statistics.median(settled)) if settled else 0,
        "latest": long_videos[0] if long_videos else None,
        "videos": long_videos,
        "descriptions": descriptions,      # every scanned upload, Shorts included
        "uploads": uploads,
        "next_page": pl.get("nextPageToken"),
        "scanned": len(ids),
    }


def deep_scan(api, activity, cfg):
    """Read older uploads' descriptions (2 API units per 50 videos) up to deep_scan_videos."""
    while activity.get("next_page") and activity["scanned"] < cfg["deep_scan_videos"]:
        pl = api.call("playlistItems", part="contentDetails", playlistId=activity["uploads"],
                      maxResults=50, pageToken=activity["next_page"])
        activity["next_page"] = pl.get("nextPageToken")
        ids = [it["contentDetails"]["videoId"] for it in pl.get("items", []) if it.get("contentDetails")]
        if not ids:
            break
        activity["scanned"] += len(ids)
        vids = api.call("videos", part="snippet", id=",".join(ids))
        for v in vids.get("items", []):
            activity["descriptions"].append((v["id"], (v.get("snippet") or {}).get("description", "")))


def collect_candidates(channel, activity, suppressed):
    """Score every email found in the channel and video descriptions."""
    sup_emails, sup_domains = suppressed
    cands = {}

    def add(email, source, ref, business, sponsor):
        if email in sup_emails or email.split("@")[1] in sup_domains:
            return
        c = cands.setdefault(email, {"sources": [], "business": False, "sponsor": False, "videos": 0,
                                     "ref": ref, "best": source})
        c["sources"].append(source)
        c["name_match"] = name_matches(email, title)
        c["business"] = c["business"] or (business and not sponsor)
        c["sponsor"] = c["sponsor"] or sponsor
        if source == "video_description":
            c["videos"] += 1
        if source == "channel_description":
            c["best"], c["ref"] = source, ref

    cid = channel["id"]
    title = (channel.get("snippet") or {}).get("title", "")
    for email, biz, spon in find_emails((channel.get("snippet") or {}).get("description", "")):
        add(email, "channel_description", f"https://www.youtube.com/channel/{cid}", biz, spon)
    for vid, desc in activity["descriptions"]:
        for email, biz, spon in find_emails(desc):
            add(email, "video_description", f"https://www.youtube.com/watch?v={vid}", biz, spon)
    return cands


def confidence_of(email, c):
    if c["best"] in ("channel_description", "own_website"):
        return "high"
    if c.get("sponsor"):
        return "low"   # appears next to sponsor/promo wording: probably not the creator's
    custom = email.split("@")[1] not in FREE_PROVIDERS
    if c["best"] in ("video_description", "website_other_domain") and custom and not c.get("name_match"):
        return "low"   # company address unrelated to the channel: sponsor, employer or guest
    if c["business"] or c["videos"] >= 2:
        return "medium"
    return "low"   # one unexplained mention in one video


def fetch_page(url, cfg, robots):
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    ua = cfg["user_agent"]
    if base not in robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=cfg["website_timeout_seconds"]) as r:
                rp.parse(r.read(200_000).decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            rp.parse(["User-agent: *", "Disallow: /"] if e.code in (401, 403) else [])
        except Exception:
            rp.parse([])          # unreachable robots.txt -> treat as allowed
        robots[base] = rp
    if not robots[base].can_fetch(ua, url):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=cfg["website_timeout_seconds"]) as r:
        if "html" not in (r.headers.get("Content-Type") or "text/html"):
            return None
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read(1_500_000).decode(charset, "replace")


def scan_website(sites, cfg):
    """sites = [(url, guessed)]. Visit the page, then contact/about pages it links to,
    then common contact paths; stop at the first page with an email.
    Returns [(email, page, same_domain, guessed)]."""
    robots, found = {}, []
    for url, guessed in sites:
        site_host = host_of(url)
        parsed = urllib.parse.urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        queue, seen, visited = [url], set(), 0
        while queue and visited < cfg["max_site_pages"]:
            page = queue.pop(0)
            key = page.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            visited += 1
            try:
                html = fetch_page(page, cfg, robots)
            except Exception:
                html = None
            if not html:
                if visited == 1:  # page missing or blocked: still try the usual contact paths
                    queue.extend(base + p for p in FALLBACK_PATHS)
                continue
            emails = [e for e, _, _ in find_emails(html)]
            emails += [e.lower() for e in cloudflare_emails(html) if is_usable_email(e.lower())]
            if emails:
                for e in dict.fromkeys(emails):
                    dom = e.split("@")[1]
                    same = dom == site_host or site_host.endswith("." + dom) or dom in FREE_PROVIDERS
                    found.append((e, page, same, guessed))
                break
            if visited == 1:      # first page: queue its contact-like links, then common paths
                links = []
                for href in HREF_RE.findall(html):
                    full = urllib.parse.urljoin(page, href).split("#")[0]
                    if host_of(full) == site_host and CONTACT_LINK.search(urllib.parse.urlparse(full).path):
                        links.append(full)
                queue.extend(list(dict.fromkeys(links))[:3])
                queue.extend(base + p for p in FALLBACK_PATHS)
    return found


def build_row(channel, activity, keyword, email, conf, cands, found_at):
    sn, st = channel.get("snippet") or {}, channel.get("statistics") or {}
    subs = int(st.get("subscriberCount", 0))
    latest = activity.get("latest") or {}
    c = cands.get(email, {}) if email else {}
    others = [e for e in cands if e != email]
    return {
        "email": email or "",
        "niche": activity.get("niche", ""),
        "confidence": conf or "",
        "email_source": c.get("best", ""),
        "source_ref": c.get("ref", ""),
        "email_type": ("free" if email.split("@")[1] in FREE_PROVIDERS else "custom_domain") if email else "",
        "other_emails": " ".join(others),
        "mentions_about_email": "yes" if ABOUT_EMAIL_HINT.search(sn.get("description", "")) else "",
        "channel_title": sn.get("title", ""),
        "channel_url": f"https://www.youtube.com/channel/{channel['id']}",
        "about_url": f"https://www.youtube.com/channel/{channel['id']}/about",
        "subscribers": subs,
        "total_videos": int(st.get("videoCount", 0)),
        "country": (sn.get("country") or "").upper(),
        "email_rules": email_rules(sn.get("country"), email or ""),
        "long_uploads_30d": activity["long_uploads_30d"],
        "median_views": activity["median_views"],
        "views_per_sub": round(activity["median_views"] / subs, 3) if subs else "",
        "latest_video_title": latest.get("title", ""),
        "latest_video_url": f"https://www.youtube.com/watch?v={latest['id']}" if latest else "",
        "latest_video_views": latest.get("views", ""),
        "found_via_keyword": keyword,
        "found_at": found_at,
    }


def enrich(api, con, cfg, suppressed, out):
    """Check every pending channel; write leads / manual-check rows to the DB."""
    pending = con.execute("SELECT channel_id, keyword FROM pending ORDER BY added_at").fetchall()
    keyword_of = dict(pending)
    ids = [p[0] for p in pending]
    print(f"\nChecking {len(ids)} channels...")
    website_jobs = []   # (channel, activity, keyword, cands, urls)
    stats = {"rejected": 0, "qualified": 0, "deep_scanned": 0}
    errors_in_row = 0
    try:
        for start in range(0, len(ids), 50):
            batch = ids[start:start + 50]
            data = api.call("channels", part="snippet,statistics,contentDetails,topicDetails", id=",".join(batch))
            by_id = {it["id"]: it for it in data.get("items", [])}
            for cid in batch:
                ch = by_id.get(cid)
                if not ch:
                    mark_checked(con, cid, "not_found")
                    continue
                reason = reject_reason(ch, cfg)
                kw = keyword_of.get(cid, "")
                niche = niche_of(ch, kw, cfg)
                if not reason and not niche:
                    if kw.startswith(("featured by", "followed by", "commented on")):
                        reason = "off_niche"     # found without search and not in your niches
                    else:
                        niche = ""               # found by an earlier niche search: keep
                if reason:
                    mark_checked(con, cid, "rejected:" + reason)
                    stats["rejected"] += 1
                    continue
                try:
                    activity = analyze_videos(api, ch, cfg)
                    errors_in_row = 0
                except ApiError as e:
                    errors_in_row += 1
                    if errors_in_row >= 5:
                        raise
                    print(f"  skipped {cid}: {e}")
                    mark_checked(con, cid, "error")
                    continue
                if not activity or activity["long_uploads_30d"] < cfg["min_long_uploads_30d"]:
                    mark_checked(con, cid, "rejected:inactive")
                    stats["rejected"] += 1
                    continue
                activity["niche"] = niche
                cands = collect_candidates(ch, activity, suppressed)
                best = pick_best(cands)
                if (not best or best[1] == "low") and cfg["deep_scan_videos"] > activity["scanned"]:
                    deep_scan(api, activity, cfg)          # would go to the manual list: dig deeper first
                    cands = collect_candidates(ch, activity, suppressed)
                    best = pick_best(cands)
                    stats["deep_scanned"] += 1
                stats["qualified"] += 1
                sites = []
                if not best or best[1] != "high":
                    sites = [(u, False) for u in own_site_urls(
                        (ch.get("snippet") or {}).get("description", ""), cfg["skip_link_domains"])]
                    if cfg["guess_site_from_videos"]:
                        known = {host_of(u) for u, _ in sites}
                        sites += [(u, True) for u in guess_own_sites(
                            (ch.get("snippet") or {}).get("title", ""), activity["descriptions"],
                            cfg["skip_link_domains"]) if host_of(u) not in known]
                if cfg["fetch_websites"] and sites:
                    website_jobs.append((ch, activity, keyword_of.get(cid, ""), cands, sites))
                    mark_checked(con, cid, "website_pending")
                else:
                    save_result(con, ch, activity, keyword_of.get(cid, ""), cands, best, out, cfg)
            con.commit()
            api.persist()
            print(f"  {min(start + 50, len(ids))}/{len(ids)} checked | qualified {stats['qualified']} "
                  f"| leads {len(out['leads'])} | API units {api.units}")
    except QuotaExhausted as e:
        print(f"  stopped early: {e}. Unchecked channels stay queued for the next run.")
    finally:
        con.commit()

    if website_jobs:
        print(f"\nVisiting {len(website_jobs)} creator websites...")
        with ThreadPoolExecutor(max_workers=cfg["website_workers"]) as pool:
            results = list(pool.map(lambda job: scan_website(job[4], cfg), website_jobs))
        sup_emails, sup_domains = suppressed
        for (ch, activity, kw, cands, _), site_emails in zip(website_jobs, results):
            for email, page, same, guessed in site_emails:
                if email in sup_emails or email.split("@")[1] in sup_domains:
                    continue
                # own_website = linked from the channel description -> high.
                # guessed_site / other-domain emails -> medium.
                label = ("guessed_site" if guessed else "own_website") if same else "website_other_domain"
                c = cands.setdefault(email, {"sources": [], "business": True, "sponsor": False, "videos": 0,
                                             "ref": page, "best": label})
                c["sources"].append(label)
                c["name_match"] = name_matches(email, (ch.get("snippet") or {}).get("title", ""))
                c["business"] = True
                c["sponsor"] = False if label != "website_other_domain" else c["sponsor"]
                if label == "own_website" and c["best"] != "channel_description":
                    c["best"], c["ref"] = "own_website", page
            save_result(con, ch, activity, kw, cands, pick_best(cands), out, cfg)
        con.commit()
    return stats


def pick_best(cands):
    if not cands:
        return None
    rank = {"high": 3, "medium": 2, "low": 1}
    scored = []
    for email, c in cands.items():
        conf = confidence_of(email, c)
        scored.append((rank[conf], c["business"], len(c["sources"]), email, conf))
    scored.sort(reverse=True)
    return scored[0][3], scored[0][4]


def save_result(con, ch, activity, keyword, cands, best, out, cfg):
    found_at = iso(now_utc())
    cid = ch["id"]
    if best and best[1] in ("high", "medium"):
        ec = email_country(best[0])
        allowed = [c.upper() for c in cfg["countries"]]
        allowed += (["UK"] if "GB" in allowed else []) + (["EU"] if set(allowed) & EU_COUNTRIES else [])
        if ec and cfg["countries"] and ec not in allowed and not (ch.get("snippet") or {}).get("country"):
            mark_checked(con, cid, f"rejected:email_country_{ec}")
            return
        h = hashlib.sha256(best[0].encode()).hexdigest()
        if con.execute("SELECT 1 FROM exported WHERE email_hash=?", (h,)).fetchone():
            mark_checked(con, cid, "duplicate_email")
            return
        con.execute("INSERT INTO exported VALUES(?, ?)", (h, found_at))
        row = build_row(ch, activity, keyword, best[0], best[1], cands, found_at)
        con.execute("INSERT OR REPLACE INTO leads VALUES(?, ?, ?)", (cid, found_at, json.dumps(row)))
        out["leads"].append(row)
        mark_checked(con, cid, "lead")
    else:
        row = build_row(ch, activity, keyword, None, None, cands, found_at)
        if best:   # only a low-confidence email: keep it as a hint for manual review
            row["other_emails"] = " ".join(cands)
        con.execute("INSERT OR REPLACE INTO manual VALUES(?, ?, ?)", (cid, found_at, json.dumps(row)))
        out["manual"].append(row)
        mark_checked(con, cid, "manual")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

FIELDS = ["email", "niche", "confidence", "email_source", "source_ref", "email_type", "other_emails",
          "mentions_about_email", "channel_title", "channel_url", "about_url", "subscribers",
          "total_videos", "country", "email_rules", "long_uploads_30d", "median_views", "views_per_sub",
          "latest_video_title", "latest_video_url", "latest_video_views", "found_via_keyword",
          "found_at"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def sort_manual(rows):
    # Best manual targets first: active, mid-size, and the About tab likely has an email.
    return sorted(rows, key=lambda r: (r["mentions_about_email"] != "yes", -r["long_uploads_30d"],
                                       -int(r["subscribers"] or 0)))


def write_outputs(con, cfg, out, stamp):
    os.makedirs(cfg["output_dir"], exist_ok=True)
    paths = []
    if out["leads"]:
        p = os.path.join(cfg["output_dir"], f"leads_{stamp}.csv")
        write_csv(p, sorted(out["leads"], key=lambda r: ({"high": 0, "medium": 1}[r["confidence"]], -r["long_uploads_30d"])))
        paths.append(p)
    if out["manual"]:
        p = os.path.join(cfg["output_dir"], f"manual_check_{stamp}.csv")
        write_csv(p, sort_manual(out["manual"]))
        paths.append(p)
    all_leads = [json.loads(r[0]) for r in con.execute("SELECT data FROM leads ORDER BY found_at DESC")]
    all_manual = [json.loads(r[0]) for r in con.execute(
        "SELECT data FROM manual WHERE channel_id NOT IN (SELECT channel_id FROM manual_done) ORDER BY found_at DESC")]
    write_csv(os.path.join(cfg["output_dir"], "all_leads.csv"), all_leads)
    write_csv(os.path.join(cfg["output_dir"], "all_manual_check.csv"), sort_manual(all_manual))
    return paths, len(all_leads), len(all_manual)


# --------------------------------------------------------------------------- #
# Review helper: python3 creator_leads.py --review
# A local page that walks you through the manual list. You click YouTube's
# "View email address" button yourself; the page does everything else.
# --------------------------------------------------------------------------- #

REVIEW_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Email reveal queue</title>
<style>
:root{--bg:#f5f4f0;--card:#fff;--ink:#1c1c1a;--muted:#6d6b65;--line:#e3e0d8;--accent:#d4442a;--ok:#2d7a4c}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--card:#1e1e1c;--ink:#edebe5;--muted:#9b988f;--line:#34332f;--accent:#ff6a4d;--ok:#5cc38a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:28px 16px 64px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
h1{font-size:18px;margin:0}
.muted{color:var(--muted);font-size:13px}
.meter{margin-top:14px}
.bar{height:6px;background:var(--line);border-radius:3px;margin:6px 0 20px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}
.title{font-size:20px;font-weight:650;margin:0 0 4px;overflow-wrap:anywhere}
.badge{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--accent);color:var(--accent)}
a{color:inherit}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
button{font:inherit;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:10px 14px;cursor:pointer}
button:hover{border-color:var(--muted)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
input{flex:1;min-width:200px;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
#toast{min-height:22px;font-size:13px;margin:10px 0 0}
.empty{text-align:center;color:var(--muted);padding:40px 12px}
kbd{font:12px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-radius:4px;padding:0 5px}
.help{margin-top:22px}
</style></head><body><main>
<header><h1>Email reveal queue</h1><span class="muted" id="left"></span></header>
<div class="meter muted">Revealed today: <b id="today">0</b> / ~<span id="target">15</span>
  &middot; YouTube limits reveals per account per day</div>
<div class="bar"><i id="fill"></i></div>
<div id="app"><div class="card empty">Loading&hellip;</div></div>
<p id="toast"></p>
<p class="muted help"><kbd>O</kbd> open About page &middot; paste the email, <kbd>Enter</kbd> saves and opens the next channel
&middot; <kbd>N</kbd> no email listed &middot; <kbd>S</kbd> skip. All About pages open in the same tab.</p>
</main>
<script>
const EMAIL=/[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}/;
let queue=[], today=0, target=15;
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=n=>Number(n||0).toLocaleString();

function meter(){
  $("#today").textContent=today; $("#target").textContent=target;
  $("#fill").style.width=Math.min(100,today/target*100)+"%";
  $("#left").textContent=queue.length+" in queue";
  if(today>=target) toast("You've reached today's target. YouTube may start refusing reveals - continue tomorrow.","err");
}
function toast(t,kind){const el=$("#toast");el.textContent=t;el.style.color=kind==="err"?"var(--accent)":"var(--ok)";}
function openAbout(){const c=queue[0]; if(c) window.open(c.about_url,"yt_about");}

function render(){
  meter();
  const c=queue[0];
  if(!c){$("#app").innerHTML='<div class="card empty">Queue empty. Run the script again tomorrow for new channels.</div>';return;}
  $("#app").innerHTML=`<div class="card">
    <p class="title">${esc(c.channel_title)}</p>
    <div class="muted">${c.niche?esc(c.niche.replace(/_/g," "))+" &middot; ":""}${fmt(c.subscribers)} subscribers &middot; ${esc(c.long_uploads_30d)} long uploads in 30 days &middot; median ${fmt(c.median_views)} views
      ${c.mentions_about_email==="yes"?' &middot; <span class="badge">says email is on About</span>':''}</div>
    ${c.latest_video_url?`<div class="muted" style="margin-top:6px">Latest: <a href="${esc(c.latest_video_url)}" target="_blank" rel="noopener">${esc(c.latest_video_title)}</a></div>`:""}
    ${c.other_emails?`<div class="muted" style="margin-top:6px">Low-confidence find (often a sponsor): ${esc(c.other_emails)}</div>`:""}
    <div class="row"><button class="primary" id="open">Open About page</button></div>
    <div class="row"><input id="email" type="text" placeholder="Paste the revealed email" autocomplete="off" spellcheck="false"><button id="save">Save</button></div>
    <div class="row"><button id="none">No email listed</button><button id="skip">Skip for now</button></div>
  </div>`;
  $("#open").onclick=openAbout;
  $("#save").onclick=save;
  $("#none").onclick=noEmail;
  $("#skip").onclick=skip;
  $("#email").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();save();}});
  $("#email").focus();
}

async function post(body){
  const r=await fetch("/api/resolve",{method:"POST",headers:{"Content-Type":"application/json","X-Review":"1"},body:JSON.stringify(body)});
  return r.json();
}
// Move on immediately (so the next About page opens inside the click/keypress),
// then confirm with the local server; put the card back if anything failed.
function advance(){ queue.shift(); render(); if(queue.length) openAbout(); }
function restore(c,text){ queue.unshift(c); render(); toast(text,"err"); }

function save(){
  const c=queue[0]; if(!c) return;
  const m=($("#email").value||"").match(EMAIL);
  if(!m){toast("That doesn't look like an email address.","err");return;}
  const email=m[0].toLowerCase();
  advance();
  post({channel_id:c.channel_id,action:"save",email}).then(d=>{
    if(d.ok){today=d.revealed_today;meter();toast("Saved "+email+" for "+c.channel_title);}
    else if(d.retry){restore(c,d.error);}
    else{toast(d.error,"err");}
  }).catch(()=>restore(c,"Couldn't reach the script. Is it still running?"));
}
function noEmail(){
  const c=queue[0]; if(!c) return;
  advance();
  post({channel_id:c.channel_id,action:"none"}).then(()=>toast("Marked "+c.channel_title+" as no email."))
    .catch(()=>restore(c,"Couldn't reach the script. Is it still running?"));
}
function skip(){ if(queue.length>1){queue.push(queue.shift()); render(); openAbout();} }

document.addEventListener("keydown",e=>{
  if(e.target.tagName==="INPUT"||e.metaKey||e.ctrlKey||e.altKey) return;
  const k=e.key.toLowerCase();
  if(k==="o") openAbout(); else if(k==="n") noEmail(); else if(k==="s") skip();
});

fetch("/api/state").then(r=>r.json()).then(d=>{queue=d.queue;today=d.revealed_today;target=d.daily_target;render();})
  .catch(()=>{$("#app").innerHTML='<div class="card empty">Couldn\'t load the queue.</div>';});
</script></body></html>
"""


def local_day():
    return datetime.now().strftime("%Y-%m-%d")


def review_queue(con):
    rows = []
    for cid, data in con.execute(
            "SELECT channel_id, data FROM manual WHERE channel_id NOT IN (SELECT channel_id FROM manual_done)"):
        row = json.loads(data)
        row["channel_id"] = cid
        rows.append(row)
    return sort_manual(rows)


def export_reviewed(con, cfg):
    """Refresh all_leads.csv, all_manual_check.csv and today's revealed_<date>.csv."""
    os.makedirs(cfg["output_dir"], exist_ok=True)
    all_leads = [json.loads(r[0]) for r in con.execute("SELECT data FROM leads ORDER BY found_at DESC")]
    write_csv(os.path.join(cfg["output_dir"], "all_leads.csv"), all_leads)
    write_csv(os.path.join(cfg["output_dir"], "all_manual_check.csv"), review_queue(con))
    day = now_utc().strftime("%Y-%m-%d")
    revealed = [r for r in all_leads if r.get("email_source") == "about_page" and r.get("found_at", "").startswith(day)]
    write_csv(os.path.join(cfg["output_dir"], f"revealed_{day}.csv"), revealed)


REVEAL_FIELDS = ["channel_id", "action", "email", "at"]


def log_reveal(cfg, cid, action, email):
    path = cfg.get("reveals_file")
    if not path:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVEAL_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"channel_id": cid, "action": action, "email": email, "at": iso(now_utc())})


def import_reveals(con, cfg, suppressed):
    """Apply reveal decisions logged in reveals.csv (e.g. made on another computer)."""
    path = cfg.get("reveals_file")
    if not path or not os.path.exists(path):
        return 0
    applied = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cid = (row.get("channel_id") or "").strip()
            if not cid or con.execute("SELECT 1 FROM manual_done WHERE channel_id=?", (cid,)).fetchone():
                continue
            if not con.execute("SELECT 1 FROM manual WHERE channel_id=?", (cid,)).fetchone():
                continue
            res = resolve_manual(con, cfg, suppressed, cid, row.get("action"), row.get("email", ""), log=False)
            applied += 1 if res.get("ok") else 0
    return applied


def resolve_manual(con, cfg, suppressed, cid, action, email="", log=True):
    """Apply one decision from the review page. Returns a JSON-able dict."""
    row = con.execute("SELECT data FROM manual WHERE channel_id=?", (cid,)).fetchone()
    if not row:
        return {"ok": False, "retry": False, "error": "Channel not found (older than 30 days?)."}
    now = iso(now_utc())
    if action == "none":
        con.execute("INSERT OR REPLACE INTO manual_done VALUES(?, ?, ?)", (cid, now, "no_email"))
        con.commit()
        export_reviewed(con, cfg)
        return {"ok": True}
    if action != "save":
        return {"ok": False, "retry": True, "error": "Unknown action."}

    m = EMAIL_RE.search(email or "")
    email = m.group(0).lower() if m else ""
    if not email or not is_usable_email(email):
        return {"ok": False, "retry": True, "error": "That doesn't look like a usable email address."}
    sup_emails, sup_domains = suppressed
    if email in sup_emails or email.split("@")[1] in sup_domains:
        con.execute("INSERT OR REPLACE INTO manual_done VALUES(?, ?, ?)", (cid, now, "suppressed"))
        con.commit()
        export_reviewed(con, cfg)
        return {"ok": False, "retry": False, "error": f"{email} is on your suppression list - skipped."}
    h = hashlib.sha256(email.encode()).hexdigest()
    if con.execute("SELECT 1 FROM exported WHERE email_hash=?", (h,)).fetchone():
        con.execute("INSERT OR REPLACE INTO manual_done VALUES(?, ?, ?)", (cid, now, "duplicate_email"))
        con.commit()
        export_reviewed(con, cfg)
        return {"ok": False, "retry": False, "error": f"{email} was already exported for another channel - skipped."}

    lead = json.loads(row[0])
    lead.update({
        "email": email,
        "confidence": "high",
        "email_source": "about_page",
        "source_ref": lead.get("about_url", ""),
        "email_type": "free" if email.split("@")[1] in FREE_PROVIDERS else "custom_domain",
        "email_rules": email_rules(lead.get("country"), email),
        "found_at": now,
    })
    key = "reveals_" + local_day()
    count = int(meta_get(con, key, 0)) + 1
    con.execute("INSERT INTO exported VALUES(?, ?)", (h, now))
    con.execute("INSERT OR REPLACE INTO leads VALUES(?, ?, ?)", (cid, now, json.dumps(lead)))
    con.execute("INSERT OR REPLACE INTO manual_done VALUES(?, ?, ?)", (cid, now, "revealed"))
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(count)))
    con.commit()
    export_reviewed(con, cfg)
    return {"ok": True, "revealed_today": count}


def make_review_server(cfg, port):
    import http.server
    suppressed = load_suppression(cfg["suppression_file"])

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                return self._send(200, REVIEW_HTML, "text/html")
            if self.path == "/api/state":
                con = open_db(cfg["db_path"])
                try:
                    payload = {"queue": review_queue(con),
                               "revealed_today": int(meta_get(con, "reveals_" + local_day(), 0)),
                               "daily_target": cfg["daily_reveal_target"]}
                finally:
                    con.close()
                return self._send(200, json.dumps(payload))
            self._send(404, "{}")

        def do_POST(self):
            # The custom header blocks other websites from posting here (it forces a CORS preflight).
            if self.path != "/api/resolve" or self.headers.get("X-Review") != "1":
                return self._send(403, "{}")
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, "{}")
            con = open_db(cfg["db_path"])
            try:
                cid = str(body.get("channel_id", ""))
                result = resolve_manual(con, cfg, suppressed, cid, body.get("action"), body.get("email", ""))
                if result.get("ok"):
                    log_reveal(cfg, cid, body.get("action"), (body.get("email") or "").strip())
            finally:
                con.close()
            self._send(200, json.dumps(result))

    return http.server.HTTPServer(("127.0.0.1", port), Handler)


def run_review(cfg, open_browser=True):
    import threading
    import webbrowser
    con = open_db(cfg["db_path"])
    purge_old(con, cfg["retention_days"])
    import_reveals(con, cfg, load_suppression(cfg["suppression_file"]))
    waiting = len(review_queue(con))
    con.close()
    server = None
    for port in range(cfg["review_port"], cfg["review_port"] + 20):
        try:
            server = make_review_server(cfg, port)
            break
        except OSError:
            continue
    if server is None:
        print("No free local port found.")
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"{waiting} channels waiting. Review page: {url}")
    print("Keep this window open while you work. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Your reveals are saved in output/all_leads.csv.")
    finally:
        server.server_close()
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def youtube_quota_day(utc=None):
    """YouTube quotas reset at midnight Pacific time; return that calendar day."""
    utc = utc or now_utc()
    y = utc.year

    def nth_sunday(month, n):
        first = datetime(y, month, 1, tzinfo=timezone.utc)
        return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)
    dst_start = nth_sunday(3, 2) + timedelta(hours=10)    # 2am PST
    dst_end = nth_sunday(11, 1) + timedelta(hours=9)      # 2am PDT
    offset = -7 if dst_start <= utc < dst_end else -8
    return (utc + timedelta(hours=offset)).strftime("%Y-%m-%d")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_from_state(cfg):
    """Make leads.db match the committed state file. The working copy is re-extracted
    whenever state/leads.db.gz changed (e.g. another agent pushed a newer run), so nobody
    works on a stale database. Returns a short description of what happened."""
    import gzip
    import shutil
    db, gz = cfg["db_path"], cfg.get("state_file")
    marker = db + ".source"
    if not gz or not os.path.exists(gz):
        return "no state file; using local leads.db" if os.path.exists(db) else "new empty database"
    digest = file_sha256(gz)
    current = open(marker).read().strip() if os.path.exists(marker) else ""
    if os.path.exists(db) and current == digest:
        return "local leads.db is up to date with the state file"
    tmp = db + ".restore"
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    for leftover in (db + "-journal", db + "-wal", db + "-shm"):
        if os.path.exists(leftover):
            os.remove(leftover)
    os.replace(tmp, db)
    with open(marker, "w") as f:
        f.write(digest)
    return "restored leads.db from the state file"


def save_to_state(cfg):
    """Write a consistent, compressed snapshot of leads.db to the state file."""
    import gzip
    import shutil
    db, gz = cfg["db_path"], cfg["state_file"]
    os.makedirs(os.path.dirname(gz), exist_ok=True)
    snap = db + ".snapshot"
    src, dst = sqlite3.connect(db), sqlite3.connect(snap)
    src.backup(dst)
    dst.execute("VACUUM")
    dst.close()
    src.close()
    with open(snap, "rb") as f, gzip.open(gz + ".tmp", "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)
    os.replace(gz + ".tmp", gz)
    os.remove(snap)
    with open(db + ".source", "w") as f:
        f.write(file_sha256(gz))


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        print(f"(no {os.path.basename(path)} found - using built-in defaults; run with --init to create one)")
    cfg["api_key"] = cfg.get("api_key") or os.environ.get("YT_API_KEY", "")
    base = os.path.dirname(os.path.abspath(path))
    for k in ("output_dir", "db_path", "suppression_file", "reveals_file", "state_file"):
        if cfg.get(k) and not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(base, cfg[k])
    return cfg


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Find YouTube creators with public business emails.")
    ap.add_argument("--config", default=os.path.join(here, "config.json"))
    ap.add_argument("--niche", action="append", metavar="NAME",
                    help="search only this niche (repeat for several); default: active_niches in config.json")
    ap.add_argument("--list-niches", action="store_true", help="show the available niches and exit")
    ap.add_argument("--max-searches", type=int, help="use at most this many searches this run (e.g. 5 for a test)")
    ap.add_argument("--max-units", type=int, help="use at most this many API units this run")
    ap.add_argument("--no-websites", action="store_true", help="skip visiting creator websites")
    ap.add_argument("--review", action="store_true", help="open the local page for revealing manual-list emails")
    ap.add_argument("--init", action="store_true", help="write config.json and suppress.txt, then exit")
    args = ap.parse_args(argv)

    if args.init:
        if not os.path.exists(args.config):
            with open(args.config, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        sup = os.path.join(os.path.dirname(os.path.abspath(args.config)), "suppress.txt")
        if not os.path.exists(sup):
            with open(sup, "w", encoding="utf-8") as f:
                f.write("# One email or @domain per line. These are never output (opt-outs, bounces, clients).\n")
        print(f"Wrote {args.config} and {sup}. Add your API key, then run again without --init.")
        return 0

    cfg = load_config(args.config)
    if args.list_niches:
        act = set(active_niches(cfg))
        for name, n in cfg["niches"].items():
            print(f"  {'*' if name in act else ' '} {name:<20} {', '.join(n['search'])}")
        print("\n* = active. Pick for one run with --niche NAME, or set active_niches in config.json.")
        return 0
    print(f"Database: {sync_from_state(cfg)}")
    if args.review:
        return run_review(cfg)
    if args.niche:
        cfg["active_niches"] = args.niche
    niches = active_niches(cfg)
    if args.no_websites:
        cfg["fetch_websites"] = False
    if not cfg["api_key"]:
        print("No API key. Put it in config.json as \"api_key\" or set YT_API_KEY.")
        return 2

    con = open_db(cfg["db_path"])
    stuck = [r[0] for r in con.execute("SELECT channel_id FROM checked WHERE result='website_pending'")]
    for cid in stuck:
        con.execute("DELETE FROM checked WHERE channel_id=?", (cid,))
        con.execute("INSERT OR IGNORE INTO pending VALUES(?, ?, ?)", (cid, "re-check: interrupted run", iso(now_utc())))
    if stuck:
        con.commit()
        print(f"Re-queued {len(stuck)} channels left unfinished by an interrupted run.")
    purged = purge_old(con, cfg["retention_days"])
    if purged:
        print(f"Deleted {purged} stored records older than {cfg['retention_days']} days.")

    # ---- today's budget: what's left of the daily quota, minus the safety margin ----
    day = youtube_quota_day()
    used_s = int(meta_get(con, f"searches_{day}", 0))
    used_u = int(meta_get(con, f"units_{day}", 0))
    search_left = max(0, cfg["daily_search_budget"] - used_s)
    unit_left = max(0, cfg["daily_unit_budget"] - used_u)
    if args.max_searches is not None:
        search_left = min(search_left, args.max_searches)
    if args.max_units is not None:
        unit_left = min(unit_left, args.max_units)
    print(f"Niches: {', '.join(niches)}")
    print(f"Quota day {day}: already used {used_s}/{cfg['daily_search_budget']} searches and "
          f"{used_u}/{cfg['daily_unit_budget']} units. This run may use {search_left} searches, {unit_left} units.")
    if unit_left <= 20:
        print("Today's budget is used up. The quota resets at midnight Pacific time.")
        con.close()
        return 0

    suppressed = load_suppression(cfg["suppression_file"])
    merged = import_reveals(con, cfg, suppressed)
    if merged:
        print(f"Merged {merged} email reveals from {os.path.basename(cfg['reveals_file'])}.")
    api = YouTubeAPI(cfg["api_key"], unit_left, search_left)

    def persist():
        # saved continuously, so a killed run still counts what it spent today
        s_now = cfg["daily_search_budget"] if "search" in api.server_exhausted else used_s + api.searches
        u_now = cfg["daily_unit_budget"] if "units" in api.server_exhausted else used_u + api.units
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (f"searches_{day}", str(s_now)))
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (f"units_{day}", str(u_now)))
        con.commit()
    api.persist = persist
    out = {"leads": [], "manual": []}
    stamp = now_utc().strftime("%Y-%m-%d_%H%M")
    started = time.time()
    stats = {"rejected": 0, "qualified": 0, "deep_scanned": 0}

    def add_stats(s2):
        for k, v in s2.items():
            stats[k] = stats.get(k, 0) + v

    try:
        print(f"\nSearching ({search_left} searches)...")
        new = discover(api, con, cfg)
        print(f"  {new} new channels queued")
        add_stats(enrich(api, con, cfg, suppressed, out))
        while cfg["snowball"] and api.units < api.max_units - 20 and "units" not in api.server_exhausted:
            print(f"\nFinding more channels in your niches without search (featured, followed, commenters)... "
                  f"[{api.units}/{api.max_units} units used]")
            try:
                added, sources = snowball(api, con, cfg)
            except QuotaExhausted:
                break
            print(f"  {added} new channels from {sources} creators")
            if sources == 0:
                break
            add_stats(enrich(api, con, cfg, suppressed, out))
    except ApiError as e:
        print(f"\nAPI error: {e}")
        if "API key" in str(e) or "keyInvalid" in str(e) or "badRequest" in str(e):
            print("Check that the key is correct and that YouTube Data API v3 is enabled for its project.")
        elif "accessNotConfigured" in str(e) or "SERVICE_DISABLED" in str(e):
            print("Enable 'YouTube Data API v3' in Google Cloud for this key's project.")
    except KeyboardInterrupt:
        print("\nInterrupted - saving what was found so far.")
    finally:
        # record today's usage so the next run the same day only spends what's left
        s_total = cfg["daily_search_budget"] if "search" in api.server_exhausted else used_s + api.searches
        u_total = cfg["daily_unit_budget"] if "units" in api.server_exhausted else used_u + api.units
        meta_set(con, f"searches_{day}", s_total)
        meta_set(con, f"units_{day}", u_total)

    paths, total_leads, total_manual = write_outputs(con, cfg, out, stamp)
    con.close()
    mins = (time.time() - started) / 60
    print("\n" + "=" * 60)
    print(f"Done in {mins:.1f} min | this run: {api.searches} searches, {api.units} units | "
          f"today: {s_total}/{cfg['daily_search_budget']} searches, {u_total}/{cfg['daily_unit_budget']} units")
    print(f"Channels qualified: {stats['qualified']} | rejected: {stats['rejected']} "
          f"| deep-scanned: {stats.get('deep_scanned', 0)}")
    print(f"New leads with email: {len(out['leads'])} | new manual-check channels: {len(out['manual'])}")
    print(f"In the 30-day window: {total_leads} leads, {total_manual} manual-check channels")
    for p in paths:
        print(f"  -> {p}")
    print(f"  -> {os.path.join(cfg['output_dir'], 'all_leads.csv')}")
    if total_manual:
        print(f"\n{total_manual} channels wait for a manual reveal. Start the helper with:")
        print("  python3 creator_leads.py --review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
