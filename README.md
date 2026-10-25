# Atlas of Need

🏆 **Winner of Greatest Real-World Impact at SpartHill Hacks 2026**

 A world map where you click a country at war (Ukraine, Sudan) to get an AI-written humanitarian briefing, cited and dated, built only from recent ReliefWeb reports. Atlas of Need can also draft a weekly update covering the past 7 days, with every point linked to its source, for NGOs and diaspora organizations to send to their donors. That means less time researching and more time helping people in need.

## Requirements

- Python 3.10+
- An internet connection (the page loads d3, topojson and fonts from CDNs)
- For AI briefings: Node.js 24+, Bob Shell and a Bob API key (see [AI briefings](#ai-briefings-ibm-bob)). Without them the app still runs, showing source excerpts instead.

## Setup (first time)

```bash
cd AtlasOfNeed
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
source .venv/bin/activate        # if not already active
python main.py
```

Open http://localhost:3001

To use a different port: `PORT=8080 python main.py`

The server reloads automatically when you edit any `.py` file. Edits to `static/` or `data/` need only a browser refresh. Stop it with `Ctrl+C`.

## Using the map

- **Click a country** to highlight it and zoom in
- **Click it again, click the ocean, press `Esc`, or use the reset button** to zoom back out
- **Scroll** to zoom, **drag** to pan
- **Tab / Enter** to move between and select countries with the keyboard
- **Ukraine and Sudan** (in red) open a briefing panel: cited summary, sources with dates, an "Ask" box for focused questions, and official links
- **Region bubbles** appear when you zoom into Sudan or Ukraine: one per state / oblast, sized by how many recent reports mention places there, with spelling variants merged ("El Fasher", "Al-Fashir", "El-Fasher" → El Fasher, North Darfur). Hover to see the spellings found, click for the reports
- **Weekly update ↗** (in the panel) opens `/update`: a one-page, sourced situation update that a small NGO or diaspora organisation can send to donors or members. Pick the audience, optionally add your organisation's name (printed only, never sent to the AI), generate, edit any text in place, then **Copy text** for an email or **Print / Save PDF**
- **What's new · 7 days** (tab in the panel) compares the last 7 days of reports with the weeks before: newly affected areas, displacement, humanitarian access, needs and funding. Each item cites the new report and, when it updates an earlier figure, the earlier one

## AI briefings (IBM Bob)

Without Bob, briefings show the opening sentence of each source passage (`mode: "fallback"`, badge "Source excerpts · AI unavailable"). To turn on AI (one-time setup):

1. **Install Bob Shell** (needs Node.js 24+):

   ```bash
   curl -fsSL https://bob.ibm.com/download/bobshell.sh | bash     # macOS / Linux
   # Windows (PowerShell): powershell -ep Bypass 'irm -Uri "https://bob.ibm.com/download/bobshell.ps1" | iex'
   bob --version                                                  # open a new terminal if "command not found"
   ```

2. **Accept the IBM license once.** Run `bob` interactively and accept when asked, then quit. (To read the terms first: `bob --show-license`.) Until you do, every call fails with "A license agreement is required".

3. **Create an API key** at bob.ibm.com → Settings → API Keys with **Scope: Inference**. It's shown only once. A "general" key needs a team ID and won't work as is.

4. **Put the key in `.env.local`:**

   ```bash
   cp .env.example .env.local    # then edit .env.local: BOB_API_KEY=your-key   (quotes optional)
   python main.py                # prints "AI mode: IBM Bob" on startup
   ```

`.env.local` is git-ignored. Never commit the key.

Click Ukraine or Sudan: the badge should read **"AI summary · IBM Bob"**. A briefing takes about 5 seconds.

**Troubleshooting:** when Bob fails, the server prints `Bob failed, using fallback: <reason>`.

| Reason in the log | Fix |
|---|---|
| `No such file or directory: 'bob'` | Bob Shell isn't installed or isn't on PATH. Restart the server from a new terminal. |
| `A license agreement is required` | Do step 2. |
| `Invalid or expired API key` | Make a new Inference key (step 3). Also check your terminal doesn't have an old key exported: a `BOB_API_KEY` set in the shell **overrides** `.env.local`. Run `unset BOB_API_KEY` (or open a new terminal), then restart. |

**How retrieval works:** the reports are split into passages and embedded with a small local model (`BAAI/bge-small-en-v1.5` via fastembed, about 67 MB, downloaded into `.cache/` on first run). A question is embedded the same way, and the passages with the closest meaning are picked, so "is anyone starving?" finds passages about famine and food insecurity without sharing a word. If even the best passage scores below `MIN_SIMILARITY` (0.62), the question is answered with "the reports don't cover this" and Bob isn't called. The server embeds the reports at startup and caches the vectors in `.cache/embeddings/`. If the model can't be loaded (fastembed not installed, or offline on the very first run), it falls back to keyword matching.

**How Bob is used:** `briefing.py` runs `bob run` in an empty temporary folder with every tool group, MCP and subagents disabled. Bob can only summarize the numbered passages in the prompt, and any bullet without a valid citation is dropped.

## API (for the frontend)

- `GET /api/profile/{iso3}` returns a country's key figures: `{iso3, name, updated, note, sections: [{key, title, facts: [{text, quote, source, date, url}]}], not_covered}`. `quote` is the exact sentence from the source that `text` paraphrases. 404 if there's no profile.
- `GET /api/conflicts` returns `{dataset, datasetUrl, basemapNote, countries: [{iso3, name, mapId, hasBriefing, verified, officialLinks}]}`. `mapId` matches the country `id` in the d3 map (Sudan = "729").
- `POST /api/briefing` with `{"iso3": "SDN", "question": "optional"}` returns `{country, mode: "ai"|"fallback"|"no_match"|"none", answered, bullets: [{text, cites: [1,2]}], gaps, newestSource, sources: [{n, title, source, date, url}], links}`. Each number in `cites` matches a `sources[].n`.
  - `answered: false` means the reports don't cover the question. Either no passage is similar enough (`mode: "no_match"`, Bob isn't called, e.g. "what is 1+1"), or Bob read the passages and said they don't answer it (`mode: "ai"`, empty `bullets`). `gaps` explains which. General questions like "what's happening in Sudan?" get the normal briefing.

- `POST /api/changes` with `{"iso3": "SDN", "days": 7}` returns `{country, mode: "ai"|"fallback"|"none", window: {from, to}, baseline: {from, to}, newReports, earlierReports, changes: [{category: "areas"|"displacement"|"access"|"needs"|"other", text, cites}], gaps, sources: [{n, title, source, date, url, period: "new"|"earlier"}], links}`. Every change cites at least one `new` source; changes that don't are dropped.

- `POST /api/update` with `{"iso3": "SDN", "audience": "donors"|"members", "days": 7}` returns a one-page draft: `{country, mode, audience, window, headline, summary, keyFigures: [{label, text, date, cites}], this_week, needs, response, gaps, sources: [{n, title, source, date, url, kind: "report"|"figure"}]}`. Each section is a list of `{text, cites}`.

- `GET /api/places/{iso3}` returns the regions recent reports mention: `{window: {from, to}, delayDays, totalReports, regions: [{code, name, lat, lon, disputed, reports, mentions, spellings, places, latest: [{title, source, date, url}]}], source}`. Regions only: towns never get coordinates.

Interactive API docs: http://localhost:3001/docs

## Data

- `data/conflicts.json`: which countries are highlighted (from UCDP; verify each and set `verified: true`).
- `data/reports/<iso3>.json`: saved reports, each `{title, source, date, url, text}` with paragraphs separated by blank lines. Refresh them from ReliefWeb with:

  ```bash
  python scripts/fetch_reports.py          # every country with hasBriefing; or e.g. SDN UKR
  ```

  Each fetch also adds the reports to `data/archive/<iso3>.json`, which keeps every report ever seen (with `first_seen`), so "What's new" has earlier weeks to compare against. Fetch regularly (daily is ideal) to build up history.

  It reads ReliefWeb's public RSS feeds (the ReliefWeb API needs an approved appname) and keeps up to 12 English reports per country. It prefers established humanitarian agencies and needs-focused reports (displacement, food, health, access), and skips battle-event data, meeting minutes and admin notices. Each feed only covers the latest ~20 items, so reports span a few days. Report text belongs to the original publishers; we store it only to cite and link back.

## Report archive and "What's new"

`data/archive/<iso3>.json` is the history used by "What's new": every report fetched from ReliefWeb plus any added by hand. "New" means published in the last 7 days. It is compared with reports from the 45 days before that.

To add a report from any site, for example to backfill earlier weeks or cover a source ReliefWeb doesn't carry:

```bash
python scripts/add_report.py SDN https://news.un.org/en/story/2026/10/1168577
python scripts/add_report.py SDN <url> --date 2026-10-12 --source "UN News"   # if the date or publisher isn't detected
```

It prints the detected title, date and opening text so you can check them. Pages without a clear date are refused rather than guessed. Avoid round-up pages that mix several countries (e.g. "World News in Brief"), since everything in an archive file is treated as being about that country.

Bob is told to report only things that happened or were first reported in the 7-day window, to treat restated older figures as background, and never to compare figures that measure different things. Results depend on what the week's reports cover.

## Weekly update (one-page draft)

Who it's for: small NGOs and diaspora organisations that send donors or members a short, sourced update every week, which normally takes hours to put together.

- **Key figures** come straight from the verified profiles (`data/profiles/`), each with its "as of" date. The AI doesn't write them.
- **Summary, This week, Humanitarian needs, Response and funding** are written by Bob from this week's archived reports plus those figures. Every sentence must cite a source. Uncited sentences are dropped, "This week" items must cite a report from this week, and a headline containing numbers is replaced, since it can't carry a citation.
- **One page:** at most 3 / 4 / 3 / 2 bullets per section and about 330 AI-written words. Trailing bullets are trimmed if needed. Only cited sources are listed, numbered in reading order with each URL once, as one short line each (publisher, date, shortened title, site) in two columns. The full link is clickable in the PDF and written out in Copy text. Tested: 8 of 8 generated drafts printed on one A4 page.
- It's a **draft to review**: every text block is editable, and the footer says it was AI-assisted.

## Place names and region bubbles

The same place is spelled many ways in English ("El Fasher", "Al-Fashir", "El-Fasher"; "Kharkov" vs "Kharkiv"), so keyword search misses reports. `places.py` resolves the variants:

1. **Gazetteer:** `python scripts/build_gazetteer.py` downloads [GeoNames](https://www.geonames.org) (CC BY 4.0) into `data/places/<iso3>.json`: every state / oblast with one representative point, and towns with their Latin-script spelling variants. Towns get **no coordinates**.
2. **Matching:** names in report text are normalised (accents, apostrophes, hyphens, a leading "al-/el-", a trailing "state/oblast/camp" removed) and looked up. Several guards prevent false matches:
   - Macro-regions ("Kordofan", "Darfur") are skipped.
   - A one-word name followed by another capitalised word is treated as a person ("Volodymyr Zelensky").
   - Villages without population data need a location cue ("in", "near", "camp").
   - A report's own "X, West Kordofan" overrides GeoNames for small places, but not in lists.
3. **AI for the rest:** `python scripts/resolve_places.py` sends names GeoNames doesn't know, or that exist in several regions (e.g. Tawila), to IBM Bob with their sentence and the region list. Answers go to `data/places/aliases_<iso3>.json` with `reviewed: false`. **Review them:** in testing Bob placed "Kalogie" in the right state but "corrected" its spelling to a different town (Kadugli). That was fixed by hand and is noted in the file.

**Responsible-AI choices:**
- **Regions only:** bubbles are at state / oblast level, never towns or coordinates.
- **Delayed:** reports from the last 2 days are held back (`PLACES_DELAY_DAYS` in `briefing.py`), and the date range is shown on the map.
- **Disputed areas:** Abyei gets its own dashed bubble, "Abyei Area (disputed)", instead of being counted under a Sudanese state.
- **Report counts only:** bubbles count reports that mention a region, not events or casualties.

**Known limit:** a name shared with a place abroad can still slip through. In testing, a report about a strike on Sasovo in Russia matched a Ukrainian village with the same name.

## Country profiles

`data/profiles/<iso3>.json` holds hand-picked key figures for Sudan and Ukraine (people in need, displacement, food, health, education, funding...). Each fact links to a specific report from an established source (UN agencies, IPC, OHCHR, IRC, Refugees International...), is dated with that report's publication date, and carries the exact sentence it is based on. Anything we couldn't source for 2026 is listed under `not_covered` instead of being filled in.

Check every quote against its source before a demo, or after editing a profile:

```bash
python scripts/verify_profiles.py      # exit code 1 if any quote isn't found in its source
```

It downloads each source and looks for the quote. ReliefWeb blocks scripts, so ReliefWeb quotes are checked against our saved copy in `data/reports/`. If those reports have since been refreshed, they show as "UNREACHABLE: check by hand". It also flags facts older than a year.

## Tests

```bash
pip install pytest httpx
python -m pytest tests -q
```

## Project layout

```
main.py                     FastAPI backend: map page, /static files, /api/conflicts, /api/briefing
briefing.py                 Embedding retrieval, IBM Bob call, citation check, fallback
data/                       Conflict list, current reports, report archive, country profiles, raw UCDP CSV
static/world.html           Frontend: the map (d3 + topojson, Natural Earth 1:50m borders)
static/conflicts.json       UCDP conflict details shown on the map (generated, see below)
scripts/build_conflicts.py  Builds static/conflicts.json from the raw UCDP CSV
scripts/fetch_reports.py    Refreshes data/reports/ from ReliefWeb
scripts/verify_profiles.py  Checks every profile quote against its source
scripts/add_report.py       Adds a report from any URL to the archive
scripts/build_gazetteer.py  Builds data/places/ from GeoNames
scripts/resolve_places.py   Asks IBM Bob to place unknown / ambiguous names (saved for review)
places.py                   Place-name matching with spelling variants, region summaries
scripts/archive.py          Shared archive helpers (data/archive/<iso3>.json)
.env.example                Template for .env.local (BOB_API_KEY)
tests/                      Backend tests
requirements.txt            Python dependencies
```

## Map conflict data

The map highlights **every country UCDP puts at war level** (1,000+ battle-related deaths in a conflict) in the current year's preliminary data. Nothing is picked by hand, so the map follows the data when the UCDP file is refreshed. Deaths are counted **where the fighting happened**, not by nationality, using each event's location: soldiers of any army killed in Ukraine count for Ukraine, which is why Russia (~476 deaths on its own territory) isn't red. UCDP files events in Gaza and the West Bank under Israel's country code, so `TERRITORIES` in `scripts/build_conflicts.py` counts them for Palestine using each event's area (`adm_1`). As of the 1 Oct 2026 release that's 10 places: Ukraine, Yemen, Iran, Nigeria, Pakistan, Sudan, Ethiopia, Somalia, Burkina Faso and Palestine. Israel isn't red: about 30 battle deaths occurred on its own territory in 2026, even though it's a party to war-level conflicts elsewhere. Myanmar and Mali have over 1,000 deaths in total, but spread across several conflicts each below the threshold, so UCDP counts them as minor. AI briefings, What's new, weekly updates and region bubbles exist only for Sudan and Ukraine (`hasBriefing` in `data/conflicts.json`); other red countries show their UCDP conflicts and a "no briefing yet" note. The data comes from two UCDP datasets:

- **2026 so far:** the [UCDP Candidate Events Dataset](https://ucdp.uu.se/downloads/#candidate), preliminary event data released monthly (currently through 1 Oct 2026). The country card shows these numbers, labeled as preliminary.
- **2025 (final):** the [UCDP/PRIO Armed Conflict Dataset](https://ucdp.uu.se/downloads/) v26.1.

When UCDP releases a new monthly file, set `CANDIDATE_FILE` in `scripts/build_conflicts.py` to its name and rerun the script. It downloads the file (about 13 MB, git-ignored) on first run. To regenerate `static/conflicts.json`:

```bash
python scripts/build_conflicts.py
```
