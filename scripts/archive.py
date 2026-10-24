# The report archive: every report we've ever fetched or added for a country, in data/archive/<iso3>.json.
# data/reports/ holds only the current pick for briefings and is overwritten on each fetch; the archive keeps history,
# so "What's new" can compare this week's reports with earlier ones.
import json
from datetime import date
from pathlib import Path

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "data" / "archive"


def load_archive(iso3: str) -> list[dict]:
    f = ARCHIVE_DIR / f"{iso3.lower()}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def add_to_archive(iso3: str, reports: list[dict], origin: str) -> int:
    """Adds reports not already archived (matched by URL). Returns how many were new."""
    archive = load_archive(iso3)
    known = {r["url"] for r in archive}
    new = [{**r, "first_seen": date.today().isoformat(), "origin": origin} for r in reports if r["url"] not in known]
    if new:
        archive = sorted(archive + new, key=lambda r: r["date"], reverse=True)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        (ARCHIVE_DIR / f"{iso3.lower()}.json").write_text(json.dumps(archive, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(new)
