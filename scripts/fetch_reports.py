# Pulls recent reports from ReliefWeb into data/reports/<iso3>.json, the files briefing.py retrieves from.
# Uses ReliefWeb's public country RSS feeds (https://reliefweb.int/country/<iso3>/rss.xml), which carry the
# full report text. The ReliefWeb API would be nicer but now requires an approved appname.
# Fetches every country with hasBriefing in data/conflicts.json, or only the ones given:
#   python scripts/fetch_reports.py            # all briefing countries
#   python scripts/fetch_reports.py SDN UKR
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
# Final address of /country/<iso3>/rss.xml; that one redirects to plain http, which comes back empty.
FEED_URL = "https://reliefweb.int/updates/rss.xml?legacy-river=country/{iso3}"
USER_AGENT = "AtlasOfNeed/0.1 (SpartHill hackathon prototype)"
MAX_REPORTS = 12      # newest usable reports kept per country
MIN_CHARS = 400       # skip items that are just "please refer to the attached file"
MAX_CHARS = 8000      # cap per report so one long report can't crowd out the rest

# Headings after which a report is only contacts or boilerplate.
STOP_HEADINGS = re.compile(r"^(for more information|media contact|contact|about |notes to editors)", re.I)
SKIP_LINES = re.compile(r"^(please refer to the attached (file|document)s?\.?|download (the )?(full )?report.*)$", re.I)
ENGLISH_WORDS = set("the and of to in is are was were for with on that this by from has have".split())


class ReportText(HTMLParser):
    """Turns a ReliefWeb RSS description into plain paragraphs, dropping the tag header and contact sections."""

    BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "tr", "br"}

    def __init__(self):
        super().__init__()
        self.paras, self.buf, self.skip_div, self.stopped, self.in_heading = [], [], 0, False, False

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if tag == "div" and "tag" in cls.split():
            self.skip_div += 1  # "Country: Sudan", "Source: ..." header lines
        if tag in self.BLOCKS:
            self.flush()
        self.in_heading = tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or self.in_heading

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.flush()
        if tag == "div" and self.skip_div:
            self.skip_div -= 1
        if tag.startswith("h") and tag[1:].isdigit():
            self.in_heading = False

    def handle_data(self, data):
        if not self.skip_div and not self.stopped:
            self.buf.append(data)

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        self.buf = []
        if not text or self.stopped:
            return
        if self.in_heading and STOP_HEADINGS.match(text):
            self.stopped = True
            return
        if not SKIP_LINES.match(text):
            self.paras.append(text)


def to_text(description: str) -> str:
    parser = ReportText()
    parser.feed(description)
    parser.close()
    parser.flush()
    return "\n\n".join(parser.paras)


def is_english(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower()[:3000])
    return bool(words) and sum(w in ENGLISH_WORDS for w in words) / len(words) > 0.08


def truncate(text: str, limit: int) -> str:
    """Cut at a paragraph boundary so retrieval never sees half a sentence."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip()


def fetch(iso3: str) -> list[dict]:
    req = urllib.request.Request(FEED_URL.format(iso3=iso3.lower()), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())

    reports, seen = [], set()
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        text = to_text(item.findtext("description") or "")
        key = text[:500]  # some reports are reposted under a new title with the same text
        if len(text) < MIN_CHARS or not is_english(text) or title in seen or key in seen:
            continue
        seen.update((title, key))
        authors = [a.text.strip() for a in item.findall("author") if a.text]
        try:
            day = parsedate_to_datetime(item.findtext("pubDate")).date().isoformat()
        except (TypeError, ValueError):
            continue
        reports.append({
            "title": title,
            "source": ", ".join(authors) or "ReliefWeb",
            "date": day,
            "url": (item.findtext("link") or "").strip(),
            "text": truncate(text, MAX_CHARS),
        })
        if len(reports) == MAX_REPORTS:
            break
    return reports


def main(codes: list[str]):
    if not codes:
        countries = json.loads((DATA_DIR / "conflicts.json").read_text(encoding="utf-8"))["countries"]
        codes = [c["iso3"] for c in countries if c.get("hasBriefing")]
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    for iso3 in (c.upper() for c in codes):
        try:
            reports = fetch(iso3)
        except Exception as e:  # keep the existing file if ReliefWeb is unreachable
            print(f"{iso3}: fetch failed ({e}); left data/reports/{iso3.lower()}.json unchanged")
            continue
        if not reports:
            print(f"{iso3}: no usable English reports in the feed; left the existing file unchanged")
            continue
        out = REPORTS_DIR / f"{iso3.lower()}.json"
        out.write_text(json.dumps(reports, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        dates = sorted(r["date"] for r in reports)
        print(f"{iso3}: wrote {len(reports)} reports ({dates[0]} to {dates[-1]}) to {out.relative_to(ROOT)}")
    print(f"Done at {datetime.now():%Y-%m-%d %H:%M}. Restart not needed; briefings read these files on each request.")


if __name__ == "__main__":
    main(sys.argv[1:])
