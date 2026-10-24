# Atlas of Need

> Temporary README for the SpartHill 2026 MVP: a world map where you click a country at war (Ukraine, Sudan) to get an AI-written humanitarian briefing, cited and dated, built only from recent ReliefWeb reports.

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

**How Bob is used:** `briefing.py` runs `bob run` in an empty temporary folder with every tool group, MCP and subagents disabled. Bob can only summarize the numbered passages in the prompt, and any bullet without a valid citation is dropped.

## API (for the frontend)

- `GET /api/conflicts` returns `{dataset, datasetUrl, basemapNote, countries: [{iso3, name, mapId, hasBriefing, verified, officialLinks}]}`. `mapId` matches the country `id` in the d3 map (Sudan = "729").
- `POST /api/briefing` with `{"iso3": "SDN", "question": "optional"}` returns `{country, mode: "ai"|"fallback"|"none", bullets: [{text, cites: [1,2]}], gaps, newestSource, sources: [{n, title, source, date, url}], links}`. Each number in `cites` matches a `sources[].n`.

Interactive API docs: http://localhost:3001/docs

## Data

- `data/conflicts.json`: which countries are highlighted (from UCDP; verify each and set `verified: true`).
- `data/reports/<iso3>.json`: saved reports, each `{title, source, date, url, text}` with paragraphs separated by blank lines. Refresh them from ReliefWeb with:

  ```bash
  python scripts/fetch_reports.py          # every country with hasBriefing; or e.g. SDN UKR
  ```

  It reads ReliefWeb's public RSS feeds (the ReliefWeb API needs an approved appname) and keeps up to 12 English reports per country. It prefers established humanitarian agencies and needs-focused reports (displacement, food, health, access), and skips battle-event data, meeting minutes and admin notices. Each feed only covers the latest ~20 items, so reports span a few days. Report text belongs to the original publishers; we store it only to cite and link back.

## Tests

```bash
pip install pytest httpx
python -m pytest tests -q
```

## Project layout

```
main.py                     FastAPI backend: map page, /static files, /api/conflicts, /api/briefing
briefing.py                 Retrieval, IBM Bob call, citation check, fallback
data/                       Conflict list, saved reports, raw UCDP CSV
static/world.html           Frontend: the map (d3 + topojson, Natural Earth 1:50m borders)
static/conflicts.json       UCDP conflict details shown on the map (generated, see below)
scripts/build_conflicts.py  Builds static/conflicts.json from the raw UCDP CSV
scripts/fetch_reports.py    Refreshes data/reports/ from ReliefWeb
.env.example                Template for .env.local (BOB_API_KEY)
tests/                      Backend tests
requirements.txt            Python dependencies
```

## Map conflict data

The map highlights Ukraine and Sudan (the countries with briefings; see `FOCUS` in `static/world.html`). The legend and country card cite the [UCDP/PRIO Armed Conflict Dataset](https://ucdp.uu.se/downloads/) v26.1 (calendar year 2025): both are classified as **war** (1,000+ battle-related deaths in the year).

To regenerate `static/conflicts.json` after updating the CSV in `data/`:

```bash
python scripts/build_conflicts.py
```
