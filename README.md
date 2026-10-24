# Atlas of Need

> Temporary README for the current MVP: an interactive world map where you click a country to zoom in.

## Requirements

- Python 3.10+
- An internet connection (the page loads d3, topojson and fonts from CDNs)

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

The server reloads automatically when you edit `main.py`. Stop it with `Ctrl+C`.

## Using the map

- **Click a country** to highlight it and zoom in
- **Click it again, click the ocean, press `Esc`, or use the reset button** to zoom back out
- **Scroll** to zoom, **drag** to pan
- **Tab / Enter** to move between and select countries with the keyboard

## AI briefings (IBM Bob)

Without a key, briefings show source excerpts (`mode: "fallback"`). To turn on AI, install Bob Shell and start with your key:

```bash
BOB_API_KEY=your-key python main.py          # Windows: $env:BOB_API_KEY="your-key"; python main.py
```

Never commit the key.

## API (for the frontend)

- `GET /api/conflicts` returns `{dataset, datasetUrl, basemapNote, countries: [{iso3, name, mapId, hasBriefing, verified, officialLinks}]}`. `mapId` matches the country `id` in the d3 map (Sudan = "729").
- `POST /api/briefing` with `{"iso3": "SDN", "question": "optional"}` returns `{country, mode: "ai"|"fallback"|"none", bullets: [{text, cites: [1,2]}], gaps, newestSource, sources: [{n, title, source, date, url}], links}`. Each number in `cites` matches a `sources[].n`.

Interactive API docs: http://localhost:3001/docs

## Data

- `data/conflicts.json`: which countries are highlighted (from UCDP; verify each and set `verified: true`).
- `data/reports/<iso3>.json`: saved reports, each `{title, source, date, url, text}` with paragraphs separated by blank lines. Replace the SAMPLE entries with real reports.

## Tests

```bash
pip install pytest httpx
python -m pytest tests -q
```

## Project layout

```
main.py             FastAPI backend: map page + /api/conflicts + /api/briefing
briefing.py         Retrieval, IBM Bob call, citation check, fallback
data/               Conflict list and saved reports
static/world.html   Frontend: the map (d3 + topojson, Natural Earth 1:50m borders)
tests/              Backend tests
requirements.txt    Python dependencies
```
