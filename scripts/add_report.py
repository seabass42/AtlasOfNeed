# Adds one report from any website to a country's archive (data/archive/<iso3>.json), for sources the ReliefWeb feed
# doesn't carry or no longer shows, e.g. to backfill earlier weeks for "What's new".
#   python scripts/add_report.py SDN https://news.un.org/en/story/2026/10/1168577
#   python scripts/add_report.py SDN <url> --date 2026-10-12 --source "UN News"   # if detection fails or is wrong
# Prints what it found so you can check the title, date and opening text before relying on it.
import argparse
import json
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from archive import add_to_archive  # noqa: E402

USER_AGENT = "Mozilla/5.0 (Macintosh) AtlasOfNeed/0.1"
MAX_CHARS = 12000  # long reports are cut at a paragraph boundary so one can't dominate retrieval
MONTHS = "january february march april may june july august september october november december".split()
# Site names for the source field when --source isn't given.
SITES = {"news.un.org": "UN News", "who.int": "World Health Organization", "unicef.org": "UNICEF", "wfp.org": "World Food Programme",
         "rescue.org": "International Rescue Committee", "ukraine.ohchr.org": "UN Human Rights Monitoring Mission in Ukraine (OHCHR)",
         "ohchr.org": "OHCHR", "ukraine.un.org": "United Nations in Ukraine", "refugeesinternational.org": "Refugees International",
         "unhcr.org": "UNHCR", "msf.org": "Médecins Sans Frontières"}


class Article(HTMLParser):
    """Collects the title, date hints and paragraph text, preferring the page's <article>, then <main>."""

    def __init__(self):
        super().__init__()
        self.title, self.dates, self.blocks = "", [], {"article": [], "main": [], "body": []}
        self.depth = {"article": 0, "main": 0}
        self.skip = 0
        self.buf, self.in_block, self.in_title, self.in_h1, self.h1 = [], False, False, False, ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "nav", "aside", "footer", "form"):
            self.skip += 1
        if tag in self.depth:
            self.depth[tag] += 1
        if tag == "title":
            self.in_title = True
        if tag == "h1" and not self.h1:  # first headline only; some pages repeat it
            self.in_h1 = True
        if tag == "meta" and (a.get("property") in ("article:published_time", "og:published_time")
                              or a.get("name") in ("date", "dcterms.date", "DC.date.issued") or a.get("itemprop") == "datePublished"):
            self.dates.append(a.get("content", ""))
        if tag == "time" and a.get("datetime"):
            self.dates.append(a["datetime"])
        if tag in ("p", "li", "h2", "h3"):
            self.buf, self.in_block = [], True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "nav", "aside", "footer", "form") and self.skip:
            self.skip -= 1
        if tag in self.depth and self.depth[tag]:
            self.depth[tag] -= 1
        if tag == "title":
            self.in_title = False
        if tag == "h1":
            self.in_h1 = False
        if tag in ("p", "li", "h2", "h3") and self.in_block:
            self.in_block = False
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if len(text) > 60 and not self.skip:
                self.blocks["body"].append(text)
                for k in ("article", "main"):
                    if self.depth[k]:
                        self.blocks[k].append(text)

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.in_h1:
            self.h1 += data
        if self.in_block:
            self.buf.append(data)

    def text(self):
        for k in ("article", "main", "body"):
            if sum(map(len, self.blocks[k])) > 500:
                return "\n\n".join(dict.fromkeys(self.blocks[k]))  # dict.fromkeys: drop repeated paragraphs
        return "\n\n".join(dict.fromkeys(self.blocks["body"]))


def find_date(parser: Article, html_text: str) -> str | None:
    candidates = parser.dates + re.findall(r'"datePublished"\s*:\s*"([^"]+)"', html_text)
    for c in candidates:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", c.strip())
        if m:
            return m.group(1)
    m = re.search(r"\b(\d{1,2}) (" + "|".join(MONTHS) + r") (20\d\d)\b", parser.text()[:600], re.I)  # "12 October 2026"
    if m:
        return f"{m.group(3)}-{MONTHS.index(m.group(2).lower()) + 1:02d}-{int(m.group(1)):02d}"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("iso3")
    ap.add_argument("url")
    ap.add_argument("--date", help="publication date YYYY-MM-DD, if it isn't detected correctly")
    ap.add_argument("--source", help="publisher name, if the site isn't in SITES")
    ap.add_argument("--title")
    args = ap.parse_args()

    req = urllib.request.Request(args.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        html_text = r.read().decode("utf-8", "replace")
    parser = Article()
    parser.feed(html_text)
    text = parser.text()
    title = args.title or re.sub(r"\s+", " ", parser.h1 or parser.title.split("|")[0]).strip()
    day = args.date or find_date(parser, html_text)
    host = re.sub(r"^www\.", "", urllib.request.urlparse(args.url).hostname or "")
    source = args.source or SITES.get(host) or next((v for k, v in SITES.items() if host.endswith(k)), None)
    problems = [msg for ok, msg in ((day, "no date found, pass --date"), (source, "unknown site, pass --source"),
                                    (len(text) >= 400, "too little article text found")) if not ok]
    if problems:
        raise SystemExit(f"Not added: {'; '.join(problems)}")

    if len(text) > MAX_CHARS:
        text = text[:text.rfind("\n\n", 0, MAX_CHARS)]
    report = {"title": title, "source": source, "date": day, "url": args.url, "text": text}
    added = add_to_archive(args.iso3, [report], origin="manual")
    print(f"{'Added' if added else 'Already in archive'}: {title}\n  {source} · {day} · {len(text)} chars\n  {text[:200]}...")


if __name__ == "__main__":
    main()
