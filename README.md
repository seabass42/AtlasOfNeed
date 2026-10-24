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

## Project layout

```
main.py             FastAPI backend: serves the map at "/"
static/world.html   Frontend: the map (d3 + topojson, Natural Earth 1:50m borders)
requirements.txt    Python dependencies
```
