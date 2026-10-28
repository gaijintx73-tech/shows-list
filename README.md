# Houston Shows List

A single, clean web page of upcoming live-music shows in Houston —
one line per show, in date order:

```
Sat., October 24 - Superchunk @ The Secret Group
```

It's the show list from the old Space City Rock days, except nobody has
to type it by hand anymore: a small builder program reads each venue's
own calendar, tidies everything into that one-line format, drops shows
that already happened, removes duplicates, and rebuilds the page.

**Music only.** Where a venue's calendar mixes in comedy or other
programming (The Secret Group, Bayou Music Center, Stafford Centre…),
the builder filters to music. The window is today through six months
out, because the bigger shows need planning time.

## Run it

You need Python 3 (standard library only, nothing to install):

```
python3 build.py
```

That produces:

- `index.html` — the page. Open it in a browser, or drop it on any
  static host (Netlify Drop, GitHub Pages, a $5 VPS…).
- `build-report.txt` — what happened at every source: how many shows
  it found, or why it couldn't. Also printed to your screen during the
  build. **Read this after a build** — it tells you which venues went
  quiet and why.

A build takes a few minutes (some venues make the builder visit each
show's page one at a time). One broken source never stops the build;
it's reported and skipped.

## The two files you'll actually touch

### `sources.yaml` — the venues

Every venue is one block:

```yaml
  - name: The Heights Theater
    venue: The Heights Theater
    type: heights
    url: https://theheightstheater.com/
    note: "Heights. The home page lists every show…"
```

- **Add a venue:** copy a block, fill in its name and calendar URL, and
  pick the `type` that matches how its calendar is published (the
  comment at the top of `sources.yaml` explains each type). If none of
  the types fit, use `type: none` — the venue stays on the list as a
  known gap instead of silently vanishing.
- **Remove a venue:** delete its block.
- **`venue`** is the name printed on the page after the `@`. A few
  sources are one room inside a bigger venue (The Bronze Peacock inside
  House of Blues) and get their own block so they list under their own
  name.
- **`filter_music: true`** turns on the comedy/sports filter for venues
  whose calendars mix programming.
- **`note`** is for humans: where the venue is, how its calendar works,
  and — for sources that can't be read yet — exactly what's missing.
  Several beloved rooms (the DIY spaces especially) have no calendar
  the builder can read; their notes say so, and say what would fix it.

### `settings.yaml` — the knobs

City name, page title, how many months ahead to list, timezone. Plus a
style section: `font`, `background_color`, `text_color`, `accent_color`
(the month headings), `content_width`, and a separate
`dark_background_color` / `dark_text_color` pair for phones and browsers
in dark mode. Change any of them, rebuild, and the page restyles itself.

## Make it your town's list

This was built so anyone can fork it into a *[Whatever Town] Shows
List*:

1. In `settings.yaml`, change `city`, `page_title`, and `timezone`.
2. Replace the blocks in `sources.yaml` with your town's venues and
   their calendar pages. The reader types in `build.py` cover the
   common ways venues publish calendars (WordPress event calendars,
   Prekindle, Eventbrite, ThunderTix, Squarespace, embedded
   machine-readable listings, Ticketmaster's feed, and a few
   Houston-specific page shapes you'll want to swap for your own).
3. Run `python3 build.py`, read the build report, adjust.

## Honesty rules baked in

- The page only ever shows listings the builder actually fetched from
  a source in `sources.yaml` during that run. Nothing is carried over,
  guessed, or padded.
- A source that fails says so in the build report, with the reason.
  An empty page section beats a fake listing.
- Cancelled shows are dropped; shows a venue itself flags postponed
  are dropped. (One exception, documented in `sources.yaml`:
  ThunderTix stamps *all* its listings "postponed" in its data — a
  platform quirk — so there only cancellations are dropped.)

## Known gaps (Houston, October 2026)

Wiring work in late Oct 2026 covered most of the surveyed rooms — the
details live in each source's `note`. What remains unwired, and why:

- **Emmit's Place** — own site's calendar is dateless and its events
  API 404s; the Do713 venue page renders only via script, so neither
  the text-lines reader nor the embedded-data reader finds shows.
- **Goode Co. Armadillo Palace** — no single venue calendar; shows
  surface only as per-show ticket pages and aggregator listings.
- **The DIY rooms without a feed (Trip Six, White Swan Live,
  Moon Tower Inn)** — no venue calendar and no Bandsintown venue page
  that renders events; shows are social-only or per-artist pages.
- **The socials-only bars (Grand Prize Bar, Poison Girl, Boondocks)**
  — DJ nights and one-offs announced on socials; no venue calendar.
- **The parks (Discovery Green, Levy Park, Market Square Park)** —
  Discovery Green's events page serves a stale window; Levy Park's
  working feed is dominated by fitness/classes the music filter can't
  separate; Market Square Park's own list is empty and its
  programmer's calendar mixes venues with no venue filter.
- **Miller Outdoor Theatre** — its performance list renders nothing
  as text; only individual performance pages carry data, and the
  season ends mid-November.
- **Last Concert Cafe** — now a private-events rental site; no public
  shows anywhere. **Acadia Bar & Grill** — no calendar and no current
  shows found. **Black Magic Social Club** — its Bandsintown page
  renders no events. **Red Dwarf** — presumed dormant/closed.
- **Houston Underground Shows (Google Doc)** — closed as a source:
  last updated May 14, 2026, and the maintainer now posts fliers as
  images on Instagram, which has no text pickup.
- **Anderson Fair** — wired, but its site drops the builder's
  connection outright (pre-existing, Oct 2026); builds report it as
  failed until that changes.
- **Big Top Lounge** (next to the Continental Club) — shares the
  Continental's Time.ly calendar but is not covered by the
  Continental's Bandsintown pickup.
