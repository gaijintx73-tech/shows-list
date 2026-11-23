#!/usr/bin/env python3
"""
Houston Shows List — builder.

Reads settings.yaml + sources.yaml, fetches every source's calendar,
normalizes listings to (date, artist, venue), drops past shows, filters
to music, dedupes, sorts, and renders one clean static page: index.html.

Stdlib only. Run:  python3 build.py
A per-source report is printed and saved to build-report.txt.
One bad source never stops the build — when a source fails (or returns
no shows), the builder reuses that source's last good haul from
shows-cache.json (up to cache_max_age_days old, default 7) and says so
in the report. The page then stays stable through one-day feed glitches.
"""

import datetime as dt
import html as htmlmod
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
# Fuller browser-like headers: Bandsintown (and friends) 403 the minimal
# UA + Accept set. The sec-ch-ua / Sec-Fetch-* set below is what a real
# Chrome 131 navigation sends; with it, plus retries in fetch(), the
# Bandsintown venue pages answer often enough to be usable (verified
# 2026-11-10: all four failing rooms returned 200 with parseable JSON-LD
# within a few attempts). No Accept-Encoding header: urllib gets plain
# text and needs no decompression handling.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7",
      "Accept-Language": "en-US,en;q=0.9",
      "Referer": "https://www.google.com/",
      "Sec-Fetch-Dest": "document",
      "Sec-Fetch-Mode": "navigate",
      "Sec-Fetch-Site": "cross-site",
      "Sec-Fetch-User": "?1",
      "Upgrade-Insecure-Requests": "1",
      "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", '
                   '"Not_A Brand";v="24"',
      "sec-ch-ua-mobile": "?0",
      "sec-ch-ua-platform": '"Windows"'}

# ---------------------------------------------------------------- YAML (tiny)
# The two YAML files in this project use a small, regular subset of YAML:
# flat "key: value" pairs, and one list of flat dicts under "sources:".
# This parser handles exactly that subset (plus quoted strings/bools/ints),
# so the builder needs no third-party packages.

def _yaml_value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        return raw

def load_settings(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            out[k.strip()] = _yaml_value(v)
    return out

def load_sources(path):
    sources, cur = [], None
    for line in open(path, encoding="utf-8"):
        raw = line.rstrip("\n")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "sources:":
            continue
        if stripped.startswith("- "):
            cur = {}
            sources.append(cur)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        if cur is not None and ":" in stripped:
            k, v = stripped.split(":", 1)
            cur[k.strip()] = _yaml_value(v)
    return sources

# ---------------------------------------------------------------- last-good cache
# shows-cache.json remembers each source's last successful haul, so a
# one-day feed failure doesn't wipe that venue off the page. A cached
# haul is reused only when its source fails (or returns no shows) and
# the cache is at most cache_max_age_days old (default 7, settings.yaml).
CACHE_FILE = os.path.join(HERE, "shows-cache.json")

def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    cache = {}
    for name, entry in raw.items():
        shows = []
        for s in entry.get("shows", []):
            try:
                d = dt.date.fromisoformat(s["date"])
            except (KeyError, ValueError, TypeError):
                continue
            shows.append({"date": d, "artist": s.get("artist", ""),
                          "venue": s.get("venue", "")})
        cache[name] = {"as_of": entry.get("as_of", ""), "shows": shows}
    return cache

def save_cache(cache):
    raw = {}
    for name, entry in cache.items():
        raw[name] = {"as_of": entry["as_of"],
                     "shows": [{"date": s["date"].isoformat(),
                                "artist": s["artist"],
                                "venue": s["venue"]} for s in entry["shows"]]}
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=1, sort_keys=True)
        f.write("\n")

# ---------------------------------------------------------------- fetching

def fetch(url, timeout=30):
    # Some hosts (notably Bandsintown) refuse intermittently — 403 one
    # attempt, 200 the next — so retry a couple of times with a pause
    # before giving up. The last error is re-raised so the build report
    # still records the failure and the cache can cover it.
    last_exc = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except (urllib.error.HTTPError, urllib.error.URLError,
                ConnectionError) as e:
            last_exc = e
            if attempt < 4:
                time.sleep(3)
    raise last_exc

def fetch_json(url, timeout=30):
    return json.loads(fetch(url, timeout))

# ---------------------------------------------------------------- dates

MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
MONTHS.update({m[:3].lower(): i for m, i in list(MONTHS.items())[:12]})

def parse_date_flex(text, tz):
    """Parse many date string shapes into a datetime.date (in tz)."""
    if text is None:
        return None
    if isinstance(text, (int, float)):  # epoch ms (Squarespace)
        return dt.datetime.fromtimestamp(text / 1000, tz).date()
    s = str(text).strip()
    # ISO dates / datetimes
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # M/D/YYYY
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    # Month D, YYYY / Month D
    m = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", s)
    if m and m.group(1).lower() in MONTHS:
        year = int(m.group(3)) if m.group(3) else None
        return (dt.date(year, MONTHS[m.group(1).lower()], int(m.group(2)))
                if year else (MONTHS[m.group(1).lower()], int(m.group(2))))
    return None

def resolve_yearless(month_day, today):
    """(month, day) with no year -> the next occurrence on/after today-7d."""
    month, day = month_day
    for year in (today.year, today.year + 1):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            continue
        if d >= today - dt.timedelta(days=7):
            return d
    return None

# ---------------------------------------------------------------- text cleanup

PRESENTER_RE = re.compile(
    r"^(?:[A-Z][\w&'. ]{0,40}?\s+(?:presents|present)\s*:?\s+|"
    r"[A-Z][\w&'. ]{0,30}?\s+Presents\s+)", re.I)
SHOW_SUFFIX_RE = re.compile(
    r"\s*[-–—(]?\s*(early show|late show|matinee)\s*\)?\s*$", re.I)
NON_MUSIC_RE = re.compile(
    r"comedy|comedian|stand[\s-]?up|improv|open mic|at the mic|trivia|"
    r"karaoke|bingo|roast battle|storytelling|storytime|book club|author talk|"
    r"private event|closed for a private|comic con|convention|\bexpo\b|"
    r"\bcon\b|"
    r"\bvs\.?\b.*\b(rockets|astros|dynamo|texans|dash)\b", re.I)
EVENING_RE = re.compile(r"^an evening (with|of)\s+", re.I)

def clean_artist(name):
    if not name:
        return ""
    s = htmlmod.unescape(str(name))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—•·")
    if "|" in s:
        # Some venue pages glue listing meta onto the title with pipes
        # ("ATTIC | +21 | Ticketed") — keep only the name-like segments.
        junk = re.compile(r"ticketed|all ages|fest stage|"
                          r"festival|free|rsvp|presented by|"
                          r"^\s*\+?\d{2}\+?\s*$", re.I)
        parts = [p.strip() for p in s.split("|")
                 if p.strip() and not junk.search(p)]
        s = " ".join(parts)
    s = PRESENTER_RE.sub("", s).strip()
    s = EVENING_RE.sub("", s).strip()
    s = re.sub(r"^(sold out|postponed|cancell?ed)\s*[-–—:]\s*", "", s, flags=re.I)
    s = re.sub(r"\s*[-–—]\s*(sold out|postponed|cancell?ed)\s*$", "", s, flags=re.I)
    s = re.sub(r"(\s\+\d{2}\s*$|^\+\d{2}\s|\s\d{2}\+\s*$|^\d{2}\+\s)", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—")
    s = SHOW_SUFFIX_RE.sub("", s).strip()
    return s

# Never a public show, whatever the source: venue closures / private hires.
ALWAYS_DROP_RE = re.compile(r"private event|closed for|closed tonight", re.I)

def is_music(title):
    return not NON_MUSIC_RE.search(title or "")

# ---------------------------------------------------------------- strategies
# Each returns a list of {"date": date, "artist": str, "venue": str}.

def ev(date, artist, venue):
    return {"date": date, "artist": artist, "venue": venue}

def fetch_none(src, ctx):
    raise SkipSource(src.get("note", "no usable calendar"))

class SkipSource(Exception):
    pass

def fetch_tribe(src, ctx):
    base = src["url"].rstrip("/")
    out, page = [], 1
    while page <= 10:
        url = (f"{base}/wp-json/tribe/events/v1/events?per_page=100"
               f"&start_date={ctx['start']}&end_date={ctx['end']}&page={page}")
        data = fetch_json(url)
        events = data.get("events", [])
        for e in events:
            d = parse_date_flex(e.get("start_date"), ctx["tz"])
            venue = (e.get("venue") or {}).get("venue") or src["venue"]
            if d:
                out.append(ev(d, clean_artist(e.get("title", "")), src["venue"]))
        if len(events) < 100:
            break
        page += 1
    return out

def fetch_ics(src, ctx):
    text = fetch(src["url"])
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        m = re.search(r"DTSTART[^:\n]*:([0-9T]+)", block)
        s = re.search(r"SUMMARY:(.*)", block)
        if not m or not s:
            continue
        raw = m.group(1)
        d = None
        if len(raw) >= 8:
            try:
                d = dt.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
            except ValueError:
                pass
        if d:
            out.append(ev(d, clean_artist(s.group(1).strip()), src["venue"]))
    return out

def fetch_shfl(src, ctx):
    """Super Happy Fun Land's calendar is a public Google Calendar; the
    basic.ics export is its readable form. Titles are raw — multi-act
    bills are typed with literal \\n separators — and the venue books
    private rentals onto the same calendar, so rentals are dropped."""
    text = fetch(src["url"])
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold ICS continuation lines
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        m = re.search(r"DTSTART[^:\n]*:([0-9T]+)", block)
        s = re.search(r"SUMMARY:(.*)", block)
        if not m or not s:
            continue
        title = s.group(1).replace("\\n", ", ").replace("\\,", ",")
        title = title.replace("\\;", ";").strip()
        if title.lower() in ("rental", "private party", "closed"):
            continue
        raw = m.group(1)
        d = None
        if len(raw) >= 8:
            try:
                d = dt.date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
            except ValueError:
                pass
        if d:
            out.append(ev(d, clean_artist(title), src["venue"]))
    return out

def _walk_jsonld(node, found):
    if isinstance(node, dict):
        t = node.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any(str(x).endswith("Event") for x in types) and node.get("name"):
            found.append(node)
        for v in node.values():
            _walk_jsonld(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, found)

def jsonld_events_from_html(page_html):
    found = []
    for block in re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            page_html, re.S | re.I):
        block = block.strip()
        try:
            data = json.loads(block)
        except Exception:
            # some sites escape slashes oddly or trail commas; try a cleanup
            try:
                data = json.loads(block.replace("\\/", "/"))
            except Exception:
                continue
        _walk_jsonld(data, found)
    return found

def _jsonld_to_shows(nodes, src, ctx, skip_postponed=True):
    out = []
    for n in nodes:
        status = str(n.get("eventStatus", ""))
        if "Cancelled" in status:
            continue
        if skip_postponed and "Postponed" in status:
            continue
        d = parse_date_flex(n.get("startDate"), ctx["tz"])
        if isinstance(d, tuple):
            d = resolve_yearless(d, ctx["today"])
        name = str(n.get("name", ""))
        # Bandsintown names its events "Artist @ Venue"; the page line
        # appends the venue itself, so drop a trailing " @ ..." tail
        # when it names this venue — allowing for spelling differences
        # like "Eighteen Ten Ojeman" for "1810 Ojeman".
        if " @ " in name:
            head, tail = name.rsplit(" @ ", 1)
            venue_words = set(re.findall(r"[a-z0-9]+",
                                         src["venue"].lower()))
            tail_words = set(re.findall(r"[a-z0-9]+", tail.lower()))
            shared = {w for w in venue_words & tail_words
                      if len(w) >= 4 and w != "the"}
            if tail.lower() == src["venue"].lower() or shared:
                name = head
        if d:
            out.append(ev(d, clean_artist(name), src["venue"]))
    return out

def fetch_jsonld(src, ctx):
    return _jsonld_to_shows(jsonld_events_from_html(fetch(src["url"])), src, ctx)

def fetch_prekindle(src, ctx):
    # Prekindle venue pages embed one big JSON-LD array of their events.
    shows = fetch_jsonld(src, ctx)
    if shows:
        return shows
    # Fallback: pair up "name"/"startDate" in the raw embedded blob.
    page = fetch(src["url"])
    out = []
    for m in re.finditer(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*'
                         r'"startDate"\s*:\s*"(\d{4}-\d{2}-\d{2})"', page):
        d = parse_date_flex(m.group(2), ctx["tz"])
        if d:
            out.append(ev(d, clean_artist(json.loads(f'"{m.group(1)}"')),
                          src["venue"]))
    return out

def fetch_eventbrite(src, ctx):
    page = fetch(src["url"])
    urls = []
    for u in re.findall(r'https://www\.eventbrite\.com/e/[^"\'<> ]+', page):
        u = u.split("?")[0]
        if u not in urls:
            urls.append(u)
    out = []
    for u in urls[:90]:
        try:
            nodes = jsonld_events_from_html(fetch(u, timeout=20))
        except Exception:
            continue
        out.extend(_jsonld_to_shows(nodes, src, ctx))
    if not urls:
        # Some organizer pages embed the events as JSON-LD directly.
        return _jsonld_to_shows(jsonld_events_from_html(page), src, ctx)
    return out

def fetch_thundertix(src, ctx):
    # ThunderTix stamps every listing EventPostponed in its schema data
    # (a platform quirk — the venues are selling normally), so for this
    # platform only Cancelled listings are dropped.
    return _jsonld_to_shows(jsonld_events_from_html(fetch(src["url"])),
                            src, ctx, skip_postponed=False)

def fetch_squarespace(src, ctx):
    url = src["url"].rstrip("/") + "?format=json"
    data = fetch_json(url)
    out = []
    for item in data.get("items", []):
        d = parse_date_flex(item.get("startDate"), ctx["tz"])
        if d:
            out.append(ev(d, clean_artist(item.get("title", "")), src["venue"]))
    return out

def fetch_mcgonigels(src, ctx):
    page = fetch(src["url"])
    out = []
    for m in re.finditer(r"eventObjects\.push\(\s*(\{.*?\})\s*\);", page, re.S):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        d = parse_date_flex((obj.get("eventDate") or "").split(" ")[0]
                            if obj.get("eventDate") else None, ctx["tz"])
        artists = (obj.get("mainArtist") or []) + (obj.get("additionalArtists") or [])
        name = artists[0] if artists else ""
        if len(artists) > 1:
            name = name + " with " + ", ".join(artists[1:])
        if d and name:
            out.append(ev(d, clean_artist(name), src["venue"]))
    return out

def fetch_heights(src, ctx):
    """The Heights Theater home page lists shows as 'tour-date' blocks:
    weekday + day + month (year only on a few), artist in .main-head."""
    page = fetch(src["url"])
    wd = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    out, seen = [], set()
    for m in re.finditer(r'class="tour-date">(.*?)</p>', page, re.S):
        seg = page[m.start():m.start() + 1500]
        nm = re.search(r'class="main-head"[^>]*>(.*?)</span>', seg, re.S)
        if not nm:
            continue
        if re.search(r"postponed|cancell?ed", nm.group(1), re.I):
            continue  # the venue flags these in the listing itself
        name = clean_artist(nm.group(1))
        d = None
        meta = re.search(r'itemprop="startDate" content="(\d{4}-\d{2}-\d{2})"', seg)
        if meta:
            d = parse_date_flex(meta.group(1), ctx["tz"])
        else:
            raw = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
            dm = re.match(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+(\d{1,2})\s+"
                          r"([A-Za-z]{3,9})", raw)
            if dm and dm.group(3).lower() in MONTHS:
                month = MONTHS[dm.group(3).lower()]
                day, want = int(dm.group(2)), wd[dm.group(1)]
                for year in (ctx["today"].year, ctx["today"].year + 1):
                    try:
                        cand = dt.date(year, month, day)
                    except ValueError:
                        continue
                    if (cand.weekday() == want
                            and cand >= ctx["today"] - dt.timedelta(days=45)):
                        d = cand
                        break
        if d and name and (d, name.lower()) not in seen:
            seen.add((d, name.lower()))
            out.append(ev(d, name, src["venue"]))
    return out

def fetch_whiteoak(src, ctx):
    """White Oak's home page renders its shows with the TicketWeb plugin:
    .tw-section cards with .tw-name and split date spans."""
    page = fetch(src["url"])
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page,
                  flags=re.S | re.I)
    out, seen = [], set()
    for b in re.split(r'class="tw-section', page)[1:]:
        nm = re.search(r'class="tw-name"[^>]*>\s*<a[^>]*>(.*?)</a>', b, re.S)
        if not nm:
            continue
        name = clean_artist(re.sub(r"<[^>]+>", " ", nm.group(1)))
        d = None
        full = re.search(r"(January|February|March|April|May|June|July|"
                         r"August|September|October|November|December)"
                         r"\s+(\d{1,2}),\s+(\d{4})", b)
        if full:
            d = parse_date_flex(full.group(0), ctx["tz"])
        else:
            daym = re.search(r'class="tw-event-date"[^>]*>\s*(\d{1,2})\s*<', b)
            monm = re.search(r'class="tw-event-month"[^>]*>\s*([A-Za-z]+)', b)
            if daym and monm and monm.group(1).lower() in MONTHS:
                d = resolve_yearless(
                    (MONTHS[monm.group(1).lower()], int(daym.group(1))),
                    ctx["today"])
        if d and name and (d, name.lower()) not in seen:
            seen.add((d, name.lower()))
            out.append(ev(d, name, src["venue"]))
    return out

def fetch_squarespace_pages(src, ctx):
    """For Squarespace venues whose events live on individual pages linked
    from one index page: follow the links, read each page's JSON view."""
    page = fetch(src["url"])
    marker = str(src.get("link_marker", ""))
    base_path = urllib.parse.urlparse(src["url"]).path.rstrip("/")
    links = []
    for href in re.findall(r'href="([^"]+)"', page):
        full = urllib.parse.urljoin(src["url"], href)
        path = urllib.parse.urlparse(full).path
        if marker and marker in path and path.rstrip("/") != base_path:
            if full not in links:
                links.append(full)
    out = []
    for u in links[:160]:
        try:
            data = fetch_json(u.rstrip("/") + "?format=json", timeout=20)
        except Exception:
            continue
        item = data.get("item") or {}
        d = parse_date_flex(item.get("startDate"), ctx["tz"])
        if d and item.get("title"):
            out.append(ev(d, clean_artist(item["title"]), src["venue"]))
    return out

def fetch_ticketmaster(src, ctx):
    if ctx.get("tm_error"):
        raise RuntimeError(ctx["tm_error"])
    cache = ctx.setdefault("tm_events", None)
    if cache is None:
        events, page_no = [], 0
        while page_no < 4:
            cmd = ["/opt/hatch/bin/ticketmaster", "search-events",
                   "--keyword", ctx["settings"].get("city", "Houston"),
                   "--city", ctx["settings"].get("city", "Houston"),
                   "--classification-name", "Music",
                   "--start-date", ctx["start"], "--end-date", ctx["end"],
                   "--size", "200", "--page", str(page_no), "--sort", "date,asc"]
            try:
                raw = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=60).stdout
                data = json.loads(raw)
            except Exception as e:
                raise RuntimeError(f"Ticketmaster CLI failed: {e}")
            if data.get("ok") is False:
                fault = json.dumps(data)[:160]
                ctx["tm_error"] = f"Ticketmaster feed unavailable: {fault}"
                raise RuntimeError(ctx["tm_error"])
            batch = data.get("_embedded", {}).get("events", [])
            events.extend(batch)
            total_pages = data.get("page", {}).get("totalPages", 1)
            page_no += 1
            if page_no >= total_pages or not batch:
                break
        ctx["tm_events"] = events
        cache = events
    wanted = [w.strip().lower() for w in
              str(src.get("tm_venues", "")).split(";") if w.strip()]
    out = []
    for e in cache:
        venues = e.get("_embedded", {}).get("venues", [])
        vname = (venues[0].get("name", "") if venues else "").lower()
        if wanted and not any(w in vname or vname in w for w in wanted):
            continue
        status = e.get("dates", {}).get("status", {}).get("code", "")
        if status in ("cancelled", "postponed"):
            continue
        d = parse_date_flex(e.get("dates", {}).get("start", {}).get("localDate"),
                            ctx["tz"])
        if d:
            name = clean_artist(e.get("name", ""))
            # Ticketmaster sometimes names the event "ARTIST — (VENUE,
            # CITY)"; the page line appends the venue itself, so drop a
            # trailing parenthetical that names this venue.
            pm = re.search(r"\s*[—–-]\s*\(([^)]*)\)\s*$", name)
            if pm:
                tail_words = set(re.findall(r"[a-z0-9]+",
                                            pm.group(1).lower()))
                venue_words = {w for w in re.findall(r"[a-z0-9]+", vname)
                               if len(w) >= 4}
                if tail_words & venue_words:
                    name = name[:pm.start()].strip()
            out.append(ev(d, name, src["venue"]))
    return out

def fetch_html_events(src, ctx):
    """Generic best-effort reader for plain server-rendered events pages.
    Splits the page into chunks at headings/list items and pairs the first
    date in each chunk with its heading text. Conservative on purpose."""
    page = fetch(src["url"])
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S | re.I)
    chunks = re.split(r"(?=<h[1-4]\b|<article\b|<li\b)", page)
    out = []
    date_re = re.compile(
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"
        r"|\d{1,2}/\d{1,2}/\d{2,4}")
    for chunk in chunks:
        dm = date_re.search(chunk)
        if not dm:
            continue
        hm = re.search(r"<h[1-4][^>]*>(.*?)</h[1-4]>", chunk, re.S | re.I)
        if not hm:
            hm = re.search(r"<a[^>]*>([^<]{3,80})</a>", chunk)
        if not hm:
            continue
        title = re.sub(r"<[^>]+>", " ", hm.group(1))
        title = re.sub(r"\s+", " ", htmlmod.unescape(title)).strip()
        if (len(title) < 3 or title.lower() in ("tickets", "more info",
                                                "calendar", "events", "home")
                or title.lower().startswith(("skip to", "archives:", "search",
                                             "menu", "coming up"))):
            continue
        d = parse_date_flex(dm.group(0), ctx["tz"])
        if isinstance(d, tuple):
            d = resolve_yearless(d, ctx["today"])
        if d:
            out.append(ev(d, clean_artist(title), src["venue"]))
    # Dedupe identical (date,title) pairs this generic pass often doubles.
    seen, uniq = set(), []
    for s in out:
        k = (s["date"], s["artist"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return uniq

def _text_lines(page_html):
    """A page reduced to its visible text, one stripped line per element.
    Several listing pages (Bandsintown, DICE, ATG, Do713, Warehouse Live)
    lay their shows out as short text lines rather than as headings or
    machine-readable data, so these readers work from lines, not markup."""
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page_html,
                  flags=re.S | re.I)
    page = re.sub(r"<[^>]+>", "\n", page)
    lines = []
    for raw in page.split("\n"):
        s = re.sub(r"\s+", " ", htmlmod.unescape(raw)).strip()
        if s:
            lines.append(s)
    return lines

def fetch_dice(src, ctx):
    """DICE venue pages list cards as 'Title' then 'Www D Mon' (yearless),
    or the two on one line. The title is the card's own line; venue and
    price lines follow the date and are ignored."""
    lines = _text_lines(fetch(src["url"]))
    date_only = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\s+"
                           r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$")
    combined = re.compile(r"^(.+?)\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\s+"
                          r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$")
    out, last_title = [], None
    for line in lines:
        cm = combined.match(line)
        if cm:
            d = resolve_yearless((MONTHS[cm.group(4).lower()],
                                  int(cm.group(3))), ctx["today"])
            if d:
                out.append(ev(d, clean_artist(cm.group(1)), src["venue"]))
            last_title = None
            continue
        dm = date_only.match(line)
        if dm:
            d = resolve_yearless((MONTHS[dm.group(3).lower()],
                                  int(dm.group(2))), ctx["today"])
            if d and last_title:
                out.append(ev(d, clean_artist(last_title), src["venue"]))
            last_title = None
            continue
        if (line.startswith("$") or line == "Free"
                or len(line) > 90
                or line.lower() in ("all events", "about", "gallery",
                                    "socials", "address")
                or line.lower().endswith("followers")
                or src["venue"].lower() in line.lower()):
            continue
        last_title = line
    return out

def fetch_warehouselive(src, ctx):
    """Warehouse Live's calendar page opens with a list view of cards.
    Each card carries its title as a link before its 'Www Mon DD' date
    line and again as text after it, with doors/show/price lines around
    them; the month grids below carry no such date lines, so only the
    list view is read — a rolling near-term feed, not the full six
    months. A date takes the title line after it when that line looks
    like a title, and falls back to the title link just before it."""
    lines = _text_lines(fetch(src["url"]))
    date_re = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
                         r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                         r"\s+(\d{1,2})$")
    junk = re.compile(r"^(Doors:|Show:|at |\$|Buy Tickets|Not Available|"
                      r"Event passed|\d{1,2}:\d{2}\s*[AP]M$|with\s)", re.I)
    out, last_title, age = [], None, 99
    i = 0
    while i < len(lines):
        line = lines[i]
        m = date_re.match(line)
        if m:
            d = resolve_yearless((MONTHS[m.group(2).lower()],
                                  int(m.group(3))), ctx["today"])
            title = None
            if i + 1 < len(lines) and not junk.match(lines[i + 1]) \
                    and not date_re.match(lines[i + 1]):
                title = lines[i + 1]
                i += 1
            elif last_title and age <= 3:
                title = last_title
            if d and title:
                out.append(ev(d, clean_artist(title), src["venue"]))
            last_title, age = None, 99
            i += 1
            continue
        if junk.match(line) or line.lower().endswith("followers") \
                or src["venue"].lower() in line.lower():
            age += 1
        elif len(line) <= 90:
            last_title, age = line, 0
        else:
            age += 1
        i += 1
    return out

def fetch_atg(src, ctx):
    """ATG Tickets listings (mirrored on the venue site) lay each show out
    as title / category / [tour and other lines] / venue / 'Www, Mon D,
    YYYY' date lines. The title is the line just before the category
    line, and only the 'Concert' category is kept — the same page mixes
    comedy, musicals, dance and variety shows."""
    lines = _text_lines(fetch(src["url"]))
    categories = {"Concert", "Comedy", "Musicals", "Dance", "Events",
                  "Festival", "Sports", "Theatre", "Family"}
    date_re = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+"
                         r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                         r"\s+(\d{1,2}),\s+(\d{4})$")
    out, prev, title, category = [], None, None, None
    for line in lines:
        if line in categories:
            category, title = line, prev
            prev = line
            continue
        m = date_re.match(line)
        if m:
            if category == "Concert" and title:
                d = parse_date_flex(
                    f"{m.group(2)} {m.group(3)}, {m.group(4)}", ctx["tz"])
                if d:
                    out.append(ev(d, clean_artist(title), src["venue"]))
            title, category = None, None
        prev = line
    return out

def _do713_clean_title(title, venue):
    """Do713 writes event titles as marketing copy ('X LIVE at the
    Continental Club!', 'Y STORM the Continental Club for a raucous
    Friday night!'). Strip the venue mention and the fluff around it so
    the line reads like an act name."""
    t = title.strip()
    core = re.sub(r"^the\s+", "", venue, flags=re.I)
    core_re = re.escape(core)
    # Venue phrase mid-title before a co-bill ("at the Continental Club
    # with Blondish and Sedated") — drop just the venue phrase.
    t = re.sub(r"\s+(?:live\s+)?at\s+(?:the\s+)?" + core_re +
               r"(?=\s+(?:with|featuring|feat\.?|w/)\b)", "", t, flags=re.I)
    # Otherwise cut at the venue name and strip the dangling tail
    # ("STORM the", "team up to take on the", "comes to the", ...).
    m = re.search(core_re, t, re.I)
    if m:
        t = t[:m.start()]
        t = re.sub(r"(?:\s+(?:the|at|live|to|up|for|a|an|storm|take|on|"
                   r"team|presents?|comes?|kicks?|off))+\s*$", "", t, flags=re.I)
        t = t.rstrip(" -–—:").strip()
    return t.rstrip("!").strip()

def fetch_do713(src, ctx):
    """Do713 venue pages are server-rendered Svelte: each event is an
    <h3> title line followed by a 'Www, Mon DD | h pm' date line, 10
    cards per page with a rel=next pagination link. Yearless dates are
    the next occurrence; dates in the next calendar year carry the year.
    Titles are marketing copy, so the venue mention is stripped (see
    _do713_clean_title). Follow pagination until there is no next page."""
    out, seen, visited = [], set(), set()
    url = src["url"]
    for _page in range(6):
        if url in visited:
            break
        visited.add(url)
        html = fetch(url)
        lines = _text_lines(html)
        date_re = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), "
                             r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                             r"\s+(\d{1,2})(?:,\s*(\d{4}))?\s*\|")
        for i, line in enumerate(lines):
            m = date_re.match(line)
            if not m or i == 0:
                continue
            title = lines[i - 1]
            if date_re.match(title) or len(title) < 3:
                continue
            if title.lower() in ("events", "venues", "about", "follow",
                                 "more info", "tickets", "home"):
                continue
            if m.group(4):
                d = parse_date_flex(
                    f"{m.group(2)} {m.group(3)}, {m.group(4)}", ctx["tz"])
            else:
                d = resolve_yearless(
                    (MONTHS[m.group(2).lower()], int(m.group(3))),
                    ctx["today"])
            if not d:
                continue
            name = clean_artist(_do713_clean_title(title, src["venue"]))
            key = (d, name.lower())
            if name and key not in seen:
                seen.add(key)
                out.append(ev(d, name, src["venue"]))
        nxt = None
        for tag in re.findall(r"<a\b[^>]*>", html, re.I):
            if re.search(r'rel="next', tag, re.I):
                hm = re.search(r'href="([^"]+)"', tag)
                if hm:
                    nxt = urllib.parse.urljoin(url, hm.group(1))
                break
        if not nxt or nxt in visited:
            break
        url = nxt
    return out

def _title_case(name):
    """'CAMERON PHAN' -> 'Cameron Phan'; short all-caps words (initials
    like JB, AJ, JP) stay upper."""
    return " ".join(w if (w.isupper() and len(w) <= 2) else w.title()
                    for w in name.split())

def fetch_rustic(src, ctx):
    """The Rustic's Uptown Park music-events page lists its shows as
    plain text: a 'Saturday November 21' date line, a '11:30 AM - 1:30
    PM' time line, then the act name in ALL CAPS. Read those triples —
    a rolling near-term feed (about six weeks), not the full six months.
    The same page template serves the chain's other locations, so the
    reader works for any of them given the right URL."""
    lines = _text_lines(fetch(src["url"]))
    date_re = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+"
                         r"(January|February|March|April|May|June|July|"
                         r"August|September|October|November|December)"
                         r"\s+(\d{1,2})$")
    time_re = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M"
                         r"(\s*-\s*\d{1,2}:\d{2}\s*[AP]M)?$")
    out, seen = [], set()
    i = 0
    while i + 2 < len(lines):
        m = date_re.match(lines[i])
        if m and time_re.match(lines[i + 1]):
            d = resolve_yearless(
                (MONTHS[m.group(2).lower()], int(m.group(3))), ctx["today"])
            name = clean_artist(_title_case(lines[i + 2]))
            if d and name:
                key = (d, name.lower())
                if key not in seen:
                    seen.add(key)
                    out.append(ev(d, name, src["venue"]))
            i += 3
            continue
        i += 1
    return out

def fetch_rockinghtown(src, ctx):
    """Rocking H-Town (rockinghtownlive.com/cal.html) is a hand-kept
    monthly calendar table: a month header row ('November', 'December',
    'January 2027'), then one row per day ('27th') whose cell lists
    'Artist(s) - Venue' entries separated by <br>. Keep only the entries
    whose venue matches table_venues (semicolon list, like tm_venues).
    Entries wrapped over two lines are joined back together, and a
    'Headliner : support, support' bill reads as 'Headliner w/ ...'."""
    html = fetch(src["url"])
    wanted = [w.strip().lower() for w in
              str(src.get("table_venues", "")).split(";") if w.strip()]
    months_re = (r"(January|February|March|April|May|June|July|August|"
                 r"September|October|November|December)")
    pat = re.compile(
        r'<td[^>]*colspan="2"[^>]*>\s*<big[^>]*>\s*<big[^>]*>\s*'
        + months_re + r'(?:\s+(\d{4}))?'
        r'|<font[^>]*size="\+2"[^>]*>(\d{1,2})(?:st|nd|rd|th)</font>'
        r'</b></td>\s*<td[^>]*>(.*?)</td>',
        re.S | re.I)
    out, seen = [], set()
    month = None
    for m in pat.finditer(html):
        if m.group(1):
            month = MONTHS[m.group(1).lower()]
            continue
        if month is None or not m.group(3):
            continue
        d = resolve_yearless((month, int(m.group(3))), ctx["today"])
        if not d:
            continue
        cell = re.sub(r"<br\s*/?>", "\n", m.group(4), flags=re.I)
        cell = re.sub(r"<[^>]+>", " ", cell)
        cell = htmlmod.unescape(cell)
        raw_lines = [re.sub(r"\s+", " ", l).replace("\xa0", " ").strip()
                     for l in cell.split("\n")]
        # Join wrapped entries: a line with no " - " belongs to the entry
        # whose venue sits on the next line.
        entries, buf = [], None
        for ln in raw_lines:
            if not ln:
                continue
            if " - " in ln:
                if buf:
                    ln = buf + ", " + ln
                    buf = None
                entries.append(ln)
            else:
                buf = (buf + ", " + ln) if buf else ln
        for entry in entries:
            if " - " not in entry:
                continue
            artists, venue = entry.rsplit(" - ", 1)
            venue = re.sub(r"\s*\([^)]*\)\s*$", "", venue).strip()
            if wanted and not any(w in venue.lower()
                                  or venue.lower() in w for w in wanted):
                continue
            artists = artists.strip()
            if " : " in artists:
                artists = artists.replace(" : ", " w/ ", 1)
            name = clean_artist(artists)
            key = (d, name.lower())
            if name and key not in seen:
                seen.add(key)
                out.append(ev(d, name, src["venue"]))
    return out

STRATEGIES = {
    "none": fetch_none,
    "tribe": fetch_tribe,
    "ics": fetch_ics,
    "jsonld": fetch_jsonld,
    "prekindle": fetch_prekindle,
    "eventbrite": fetch_eventbrite,
    "squarespace": fetch_squarespace,
    "mcgonigels": fetch_mcgonigels,
    "mcgonigels": fetch_mcgonigels,
    "heights": fetch_heights,
    "whiteoak": fetch_whiteoak,
    "squarespace_pages": fetch_squarespace_pages,
    "ticketmaster": fetch_ticketmaster,
    "thundertix": fetch_thundertix,
    "html": fetch_html_events,
    "shfl": fetch_shfl,
    "dice": fetch_dice,
    "warehouselive": fetch_warehouselive,
    "atg": fetch_atg,
    "do713": fetch_do713,
    "rustic": fetch_rustic,
    "rockinghtown": fetch_rockinghtown,
}

# Lower number wins when two sources list the same show — a venue's own
# calendar beats a ticketing platform, which beats the Ticketmaster feed.
PRIORITY = {"mcgonigels": 0, "tribe": 0, "squarespace": 0, "jsonld": 0,
            "heights": 0, "squarespace_pages": 0, "shfl": 0,
            "warehouselive": 0, "rustic": 0,
            "html": 1, "whiteoak": 1, "thundertix": 1, "prekindle": 2,
            "eventbrite": 2, "ics": 2, "dice": 2,
            "atg": 2, "do713": 1, "rockinghtown": 1,
            "ticketmaster": 3, "none": 9}

WEEKDAYS = ["Mon.", "Tue.", "Wed.", "Thu.", "Fri.", "Sat.", "Sun."]

def fmt_line(show):
    d = show["date"]
    return (f"{WEEKDAYS[d.weekday()]}, {d.strftime('%B')} {d.day} - "
            f"{show['artist']} @ {show['venue']}")

def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
                      else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return dt.date(y, m, day)

def main():
    settings = load_settings(os.path.join(HERE, "settings.yaml"))
    sources = load_sources(os.path.join(HERE, "sources.yaml"))
    tz = ZoneInfo(settings.get("timezone", "America/Chicago"))
    today = dt.datetime.now(tz).date()
    months = int(settings.get("months_ahead", 6))
    end = add_months(today, months)
    ctx = {"tz": tz, "today": today, "start": today.isoformat(),
           "end": end.isoformat(), "settings": settings}

    report = [f"Houston Shows List — build report",
              f"Run: {dt.datetime.now(tz).strftime('%A, %B %d, %Y %I:%M %p %Z')}",
              f"Window: {today.isoformat()} to {end.isoformat()} "
              f"({months} months)", ""]
    print("\n".join(report))

    cache = load_cache()
    try:
        max_age = int(settings.get("cache_max_age_days", 7))
    except (TypeError, ValueError):
        max_age = 7
    cached_sources = []  # (name, as_of, count) served from cache this run

    all_shows = []  # (priority, show, source_name)
    for src in sources:
        name = src.get("name", "?")
        stype = str(src.get("type", "none")).lower()
        fn = STRATEGIES.get(stype, fetch_none)
        shows = None
        failed = None
        try:
            shows = fn(src, ctx)
        except SkipSource as e:
            line = f"SKIP  {name}: {e}"
            print(line); report.append(line); continue
        except Exception as e:
            failed = f"{type(e).__name__}: {e}"
        kept = []
        if shows:
            for s in shows:
                if not s["artist"] or not s["date"]:
                    continue
                if ALWAYS_DROP_RE.search(s["artist"]):
                    continue
                if s["date"] < today or s["date"] > end:
                    continue
                if src.get("filter_music") is True and not is_music(s["artist"]):
                    continue
                # Per-source drop pattern (a regex, case-insensitive):
                # the source's own feed mixes in a sibling room or a
                # non-music party the venue name alone can't separate
                # (e.g. Big Top Lounge shows on the Continental Club's
                # Do713 page, the Rustic's DrinksGiving party).
                if src.get("drop_re") and re.search(str(src["drop_re"]),
                                                    s["artist"], re.I):
                    continue
                kept.append(s)
        used_cache = False
        if failed is not None or not kept:
            entry = cache.get(name)
            if entry and entry.get("shows"):
                try:
                    age = (today - dt.date.fromisoformat(
                        entry.get("as_of", ""))).days
                except ValueError:
                    age = 10 ** 9
                if age <= max_age:
                    cached = [s for s in entry["shows"]
                              if today <= s["date"] <= end]
                    if cached:
                        kept = cached
                        used_cache = True
                        line = (f"CACHED {name} [{stype}]: {len(kept)} shows "
                                f"from {entry['as_of']} "
                                f"({failed or 'source returned no shows'})")
                        print(line); report.append(line)
                        cached_sources.append(
                            (name, entry["as_of"], len(kept)))
        if not kept:
            if failed is not None:
                line = f"FAIL  {name} [{stype}]: {failed}"
            else:
                line = f"OK    {name} [{stype}]: 0 shows"
            print(line); report.append(line); continue
        if not used_cache:
            cache[name] = {"as_of": today.isoformat(), "shows": list(kept)}
            line = f"OK    {name} [{stype}]: {len(kept)} shows"
            print(line); report.append(line)
        prio = PRIORITY.get(stype, 5)
        for s in kept:
            all_shows.append((prio, s, name))

    # Dedupe: same date + artist + venue -> keep the best-priority source.
    best = {}
    for prio, s, source_name in sorted(all_shows, key=lambda x: x[0]):
        key = (s["date"], re.sub(r"[^a-z0-9]", "", s["artist"].lower()),
               s["venue"].lower())
        if key not in best:
            best[key] = s
    shows = sorted(best.values(), key=lambda s: (s["date"], s["artist"].lower()))

    title = settings.get("page_title", "Houston Shows List")
    font = settings.get("font", "Georgia, 'Times New Roman', serif")
    bg = settings.get("background_color", "#fff")
    fg = settings.get("text_color", "#1a1a1a")
    accent = settings.get("accent_color", "#555")
    width = settings.get("content_width", "44rem")
    dark_bg = settings.get("dark_background_color", "#141414")
    dark_fg = settings.get("dark_text_color", "#e8e8e8")
    updated = dt.datetime.now(tz).strftime("%A, %B %d, %Y")
    parts = ["""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: {font}; margin: 0;
         background: {bg}; color: {fg}; }}
  main {{ max-width: {width}; margin: 0 auto; padding: 2rem 1.25rem 4rem; }}
  h1 {{ font-size: 2rem; margin: 0 0 .25rem; letter-spacing: -.01em; }}
  .updated {{ color: #777; font-size: .9rem; margin: 0 0 2rem;
              font-style: italic; }}
  .search {{ position: sticky; top: 0; padding: .6rem 0 .2rem;
             background: {bg}; }}
  .search input {{ width: 100%; box-sizing: border-box; font: inherit;
                   font-size: 1rem; padding: .55rem .75rem;
                   border: 1px solid #bbb; border-radius: .4rem;
                   background: {bg}; color: {fg}; }}
  .search input:focus {{ outline: 2px solid {accent}; outline-offset: 1px; }}
  #match-count {{ color: #777; font-size: .9rem; margin: .35rem 0 0; }}
  h2 {{ font-size: 1.05rem; text-transform: uppercase; letter-spacing: .12em;
        color: {accent}; border-bottom: 1px solid #ddd; padding-bottom: .3rem;
        margin: 2.2rem 0 1rem; }}
  p.show {{ margin: .45rem 0; font-size: 1.05rem; line-height: 1.45; }}
  footer {{ margin-top: 3rem; color: #888; font-size: .85rem;
            border-top: 1px solid #eee; padding-top: 1rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: {dark_bg}; color: {dark_fg}; }}
    .search {{ background: {dark_bg}; }}
    .search input {{ border-color: #555; background: {dark_bg};
                     color: {dark_fg}; }}
    h2 {{ color: #aaa; border-color: #333; }}
    footer {{ border-color: #262626; }}
  }}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
<p class="updated">Updated {updated} &middot; {count} shows, {start} &ndash; {end}</p>
<div class="search">
<input type="search" id="q" placeholder="Search artists or venues&hellip;" aria-label="Search shows" autocomplete="off">
<p id="match-count" aria-live="polite"></p>
</div>
<p id="no-results" hidden>No shows match that search.</p>
""".format(title=htmlmod.escape(str(title)), updated=updated,
           count=len(shows), start=today.strftime("%B %d, %Y"),
           end=end.strftime("%B %d, %Y"), font=font, bg=bg, fg=fg,
           accent=accent, width=width, dark_bg=dark_bg, dark_fg=dark_fg)]

    cur_month = None
    for s in shows:
        mk = (s["date"].year, s["date"].month)
        if mk != cur_month:
            cur_month = mk
            parts.append(f"<h2>{s['date'].strftime('%B %Y')}</h2>")
        parts.append(f'<p class="show">{htmlmod.escape(fmt_line(s))}</p>')
    if not shows:
        parts.append("<p>No shows found in this build — see build-report.txt.</p>")
    if cached_sources:
        names = ", ".join(f"{n} (from {d})" for n, d, _ in cached_sources)
        parts.append(f"<p>Note: listings for {names} are from the last day "
                     f"those calendars loaded — the calendars were "
                     f"temporarily unavailable.</p>")
    parts.append(f"""<footer>Listings are pulled automatically from the venue
calendars in sources.yaml and shown as published there. {len(shows)} shows
from {len(sources)} sources checked.</footer>
</main>""")
    # The search filter is a plain string, not an f-string: its braces
    # would otherwise be read as f-string expressions.
    parts.append("""<script>
(function () {
  var q = document.getElementById('q');
  var shows = Array.prototype.slice.call(document.querySelectorAll('p.show'));
  var heads = Array.prototype.slice.call(document.querySelectorAll('h2'));
  var count = document.getElementById('match-count');
  var noResults = document.getElementById('no-results');
  var total = shows.length;
  function filter() {
    var needle = q.value.trim().toLowerCase();
    var visible = 0;
    shows.forEach(function (p) {
      var hit = !needle || p.textContent.toLowerCase().indexOf(needle) !== -1;
      p.style.display = hit ? '' : 'none';
      if (hit) visible++;
    });
    heads.forEach(function (h) {
      var n = h.nextElementSibling, any = false;
      while (n && n.tagName !== 'H2') {
        if (n.classList && n.classList.contains('show')
            && n.style.display !== 'none') { any = true; break; }
        n = n.nextElementSibling;
      }
      h.style.display = any ? '' : 'none';
    });
    count.textContent = needle ? visible + ' of ' + total + ' shows' : '';
    noResults.hidden = !(needle && visible === 0);
  }
  q.addEventListener('input', filter);
})();
</script>
</body>
</html>""")

    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    summary = f"\nTOTAL: {len(shows)} shows on the page."
    print(summary)
    report.append(summary)
    with open(os.path.join(HERE, "build-report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    save_cache(cache)

if __name__ == "__main__":
    main()
