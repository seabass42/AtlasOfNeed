# Backend for the Atlas of Need MVP.
# Serves the map page at "/" and the API used by the briefing panel.
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Secrets such as BOB_API_KEY live in .env.local (git-ignored). Variables already set in the shell take priority.
load_dotenv(Path(__file__).parent / ".env.local")

from briefing import build_briefing, load_conflicts, warm_up  # noqa: E402

STATIC_DIR = Path(__file__).parent / "static"
PROFILES_DIR = Path(__file__).parent / "data" / "profiles"


@asynccontextmanager
async def lifespan(app):
    # Load the embedding model and embed the reports in the background, so the server starts instantly.
    threading.Thread(target=warm_up, daemon=True).start()
    yield


app = FastAPI(title="Atlas of Need", lifespan=lifespan)
# Data files the page fetches, e.g. /static/conflicts.json (built by scripts/build_conflicts.py).
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
@app.get("/world.html", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "world.html")


@app.get("/api/profile/{iso3}")
def profile(iso3: str):
    """Humanitarian profile for one country: key figures, each with the source sentence it came from. 404 if none yet."""
    if not re.fullmatch(r"[A-Za-z]{3}", iso3):
        raise HTTPException(400, "Bad country code")
    f = PROFILES_DIR / f"{iso3.lower()}.json"
    if not f.exists():
        raise HTTPException(404, "No profile for this country")
    return json.loads(f.read_text(encoding="utf-8"))


@app.get("/api/conflicts")
def conflicts():
    """Countries to highlight. Each has iso3, name, mapId (the numeric id used by the d3 map), hasBriefing, officialLinks."""
    return load_conflicts()


class BriefingRequest(BaseModel):
    iso3: str = Field(..., description="3-letter country code, e.g. SDN")
    question: str = Field("", max_length=300)


@app.post("/api/briefing")
async def briefing(req: BriefingRequest):
    """Cited briefing for one country. mode is 'ai', 'fallback', or 'none'."""
    iso3 = req.iso3.upper()
    if not re.fullmatch(r"[A-Z]{3}", iso3):
        raise HTTPException(400, "Bad country code")
    # Bob can take several seconds; run it off the main thread so the server stays responsive.
    return await run_in_threadpool(build_briefing, iso3, req.question.strip())


if __name__ == "__main__":
    import uvicorn

    print("AI mode:", "IBM Bob (bob run)" if os.environ.get("BOB_API_KEY") else "OFF (no BOB_API_KEY) - using source excerpts fallback")
    uvicorn.run("main:app", host="127.0.0.1", port=int(os.environ.get("PORT", 3001)), reload=True)
