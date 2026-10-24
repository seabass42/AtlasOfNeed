# Checks every fact in data/profiles/*.json against its source: the "quote" must appear word for word in the page at
# "url". If the site blocks scripts (ReliefWeb and OCHA often do), the quote is checked against our saved copy of that
# report in data/reports/ instead. Also warns about facts older than a year.
#   python scripts/verify_profiles.py          # exit code 1 if any quote can't be found in a page that was reachable
import html
import json
import re
import sys
import unicodedata
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT / "data" / "profiles"
REPORTS_DIR = ROOT / "data" / "reports"
USER_AGENT = "Mozilla/5.0 (Macintosh) AtlasOfNeed/0.1 (source check)"
STALE_DAYS = 365


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag in ("p", "li", "div", "br", "h1", "h2", "h3", "h4", "tr", "td"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def normalize(text: str) -> str:
    """Ignore differences that don't change meaning: curly vs straight quotes, dash types, spacing, case."""
    text = unicodedata.normalize("NFKC", html.unescape(text))
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "}))
    return re.sub(r"\s+", " ", text).strip().lower()


def fetch_page(url: str) -> str | None:
    """Page text, or None if the site wouldn't give it to a script."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    if not body.strip():  # ReliefWeb/OCHA answer bots with an empty 202
        return None
    parser = PageText()
    parser.feed(body)
    return normalize("".join(parser.parts))


def saved_copies() -> dict[str, str]:
    copies = {}
    for f in REPORTS_DIR.glob("*.json"):
        for r in json.loads(f.read_text(encoding="utf-8")):
            copies[r.get("url", "")] = normalize(r.get("title", "") + " " + r.get("text", ""))
    return copies


def main() -> int:
    pages, saved = {}, saved_copies()
    failures = unreachable = stale = checked = 0
    for f in sorted(PROFILES_DIR.glob("*.json")):
        profile = json.loads(f.read_text(encoding="utf-8"))
        print(f"\n{profile['name']} ({f.name})")
        for section in profile["sections"]:
            for fact in section["facts"]:
                checked += 1
                url, quote = fact["url"], normalize(fact["quote"])
                if url not in pages:
                    pages[url] = fetch_page(url)
                page = pages[url]
                if page is not None:
                    status = "ok" if quote in page else "NOT FOUND"
                elif url in saved:
                    status = "ok (saved copy)" if quote in saved[url] else "NOT FOUND (saved copy)"
                else:
                    status = "UNREACHABLE"
                age = (date.today() - date.fromisoformat(fact["date"])).days
                flag = f"  [older than a year: {fact['date']}]" if age > STALE_DAYS else ""
                print(f"  {status:<22} {section['key']:<20} {fact['source'][:40]:<40} {fact['date']}{flag}")
                if status.startswith("NOT FOUND"):
                    failures += 1
                    print(f"      quote: {fact['quote'][:120]}")
                    print(f"      url:   {url}")
                elif status == "UNREACHABLE":
                    unreachable += 1
                    print(f"      check by hand: {url}")
                stale += bool(flag)
    print(f"\n{checked} facts: {checked - failures - unreachable} verified, {failures} not found, "
          f"{unreachable} unreachable, {stale} older than a year")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
