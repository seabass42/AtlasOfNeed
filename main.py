# Backend for the Atlas of Need MVP.
# Serves the map page at "/". API routes (conflicts, briefings, alerts) will be added here later.
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Atlas of Need")


@app.get("/", include_in_schema=False)
@app.get("/world.html", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "world.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=int(os.environ.get("PORT", 3001)), reload=True)
