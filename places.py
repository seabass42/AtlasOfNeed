"""Finds which regions reports mention, with spelling variants resolved to one place.

"El Fasher", "Al-Fashir" and "El-Fasher" all become El Fasher (North Darfur); "Kharkov" and "Kharkiv" become Kharkiv.
1. Names are normalized: accents, apostrophes, hyphens, a leading "al-/el-" and a trailing "state/oblast/camp..." removed.
2. They are looked up in the GeoNames gazetteer (data/places/<iso3>.json, built by scripts/build_gazetteer.py).
3. Names the gazetteer doesn't know, or that exist in several regions, can be matched by IBM Bob with
   scripts/resolve_places.py; its answers are saved in data/places/aliases_<iso3>.json for review.
Only regions (states / oblasts) are ever reported, never towns or coordinates.
"""
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

PLACES_DIR = Path(__file__).parent / "data" / "places"
ARTICLES = {"al", "el", "ad", "ar", "as", "ash", "at", "an", "az", "ed", "er", "es", "en", "ag"}
TRAILING = {"state", "oblast", "region", "province", "locality", "city", "town", "village", "camp", "district", "raion"}
# Two-word-or-longer names start with a capital; these lowercase joiners may appear inside them ("Umm al Qura").
JOINERS = {"al", "el", "ad", "ar", "as", "ash", "at", "an", "az", "ed", "er", "es", "en", "de", "of", "umm", "um", "bin", "wad"}
MAX_WORDS = 4
MIN_LEN = 4  # shorter names ("Abu", "Kas") match too much by accident
# Names that span several regions (Greater Kordofan covers three states) or clash with better-known places elsewhere.
SKIP = {"kordofan", "darfur", "greater kordofan", "greater darfur", "nile", "sudan", "ukraine", "south", "north", "east",
        "west", "central", "new york"}
# Words that may follow a place name ("El Fasher Hospital"). Any other capitalized word after a one-word match means
# it's probably a person ("Volodymyr Zelensky", "Khalil Ahmed").
FOLLOWERS = TRAILING | {"hospital", "market", "airport", "university", "road", "bridge", "prison", "station", "port",
                        "teaching", "general", "dam", "river", "valley", "area", "county", "municipality", "hromada"}
# Places GeoNames lists without population are often tiny villages whose names are also personal or group names
# (Khalil, Musa, Zaghawa). They only count after a location cue: "in Tawila", "near Kutum", "Zamzam camp".
CUES = {"in", "at", "near", "from", "to", "into", "around", "outside", "towards", "toward", "across", "through", "of"}


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[’'`ʼʻ‘]", "", s)          # Kharkivs'ka -> kharkivska
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()  # El-Fasher -> el fasher
    words = s.split()
    while len(words) > 1 and words[0] in ARTICLES:
        words = words[1:]                       # el fasher -> fasher, al fashir -> fashir
    while len(words) > 1 and words[-1] in TRAILING:
        words = words[:-1]                      # north darfur state -> north darfur
    return " ".join(words)


_cache: dict = {}


def load_index(iso3: str) -> dict | None:
    """normalized name -> candidate entries, from the gazetteer plus reviewed AI aliases. Cached until files change."""
    g_file = PLACES_DIR / f"{iso3.lower()}.json"
    a_file = PLACES_DIR / f"aliases_{iso3.lower()}.json"
    if not g_file.exists():
        return None
    stamp = (g_file.stat().st_mtime, a_file.stat().st_mtime if a_file.exists() else 0)
    if iso3 in _cache and _cache[iso3]["stamp"] == stamp:
        return _cache[iso3]
    g = json.loads(g_file.read_text(encoding="utf-8"))
    index = defaultdict(list)
    for code, r in g["regions"].items():
        for v in r["variants"] + [r["name"]]:
            index[normalize(v)].append({"kind": "region", "region": code, "name": r["name"], "pop": 10**9})
    for p in g["places"]:
        for v in p["variants"] + [p["name"]]:
            index[normalize(v)].append({"kind": "place", "region": p["region"], "name": p["name"], "pop": p["pop"]})
    if a_file.exists():
        for a in json.loads(a_file.read_text(encoding="utf-8")):
            if a.get("region") in g["regions"]:
                index[normalize(a["name"])] = [{"kind": "alias", "region": a["region"], "name": a.get("canonical") or a["name"], "pop": 10**8}]
    index = {k: v for k, v in index.items() if len(k) >= MIN_LEN}
    _cache[iso3] = {"stamp": stamp, "index": index, "regions": g["regions"], "source": g["source"]}
    return _cache[iso3]


def resolve(candidates: list[dict]) -> dict | None:
    """Pick one entry for a name: a region wins; otherwise the clearly most populous place. None if ambiguous."""
    regions = {c["region"] for c in candidates}
    if len(regions) == 1:
        return max(candidates, key=lambda c: c["pop"])
    ranked = sorted(candidates, key=lambda c: -c["pop"])
    top = ranked[0]
    runner_up = next((c for c in ranked if c["region"] != top["region"]), None)
    if top["kind"] != "place" or (top["pop"] > 0 and (runner_up is None or top["pop"] >= 3 * max(runner_up["pop"], 1))):
        return top
    return None  # same name in several regions with no clear winner: left for scripts/resolve_places.py


def candidate_spans(text: str):
    """(start, end, surface) for runs of 1-4 words that start with a capital letter, longest first at each position."""
    words = [(m.start(), m.end(), m.group()) for m in re.finditer(r"[A-Za-zÀ-ɏ’'ʼ-]+", text)]
    for i, (start, _, w) in enumerate(words):
        if not w[0].isupper() and not (w.lower().split("-")[0] in ARTICLES and i + 1 < len(words) and words[i + 1][2][0].isupper()):
            continue
        for n in range(min(MAX_WORDS, len(words) - i), 0, -1):
            chunk = words[i:i + n]
            if chunk[-1][2][0].islower() and chunk[-1][2].lower() not in TRAILING:
                continue
            if any(not w2[0].isupper() and w2.lower() not in JOINERS | TRAILING | ARTICLES for _, _, w2 in chunk[1:]):
                continue
            if any(text[a[1]:b[0]].strip() for a, b in zip(chunk, chunk[1:])):  # no punctuation between words
                continue
            yield chunk[0][0], chunk[-1][1], text[chunk[0][0]:chunk[-1][1]]


def common_words(texts: list[str]) -> set[str]:
    """Words that appear in lowercase in these reports are ordinary words ("central", "hope"), not place names."""
    counts = Counter(m.group() for t in texts for m in re.finditer(r"\b[a-z]{4,}\b", t))
    return {w for w, n in counts.items() if n >= 2}


def _next_word(text: str, end: int) -> str:
    m = re.match(r"[ \t]+([A-Za-zÀ-ɏ’'-]+)", text[end:end + 40])
    return m.group(1) if m else ""


def _prev_word(text: str, start: int) -> str:
    m = re.search(r"([A-Za-zÀ-ɏ’'-]+)[ \t]+$", text[max(0, start - 40):start])
    return m.group(1) if m else ""


def _region_after(text: str, end: int, idx: dict) -> str | None:
    """Region named right after a place, as in "Dibab, West Kordofan" or "Tawila Locality, North Darfur State"."""
    m = re.match(r",\s*((?:[A-Z][A-Za-z’'-]*\s?){1,4})", text[end:end + 60])
    if not m or re.match(r"\s*(,|and\b|or\b)", text[end + m.end():]):  # "El Fasher, Khartoum and Kordofan" is a list
        return None
    words = m.group(1).split()
    for n in range(len(words), 0, -1):  # longest match first: "North Darfur State" before "North"
        hits = [c for c in idx["index"].get(normalize(" ".join(words[:n])), []) if c["kind"] == "region"]
        if hits:
            return hits[0]["region"]
    return None


def _has_cue(text: str, start: int, surface: str) -> bool:
    before = re.findall(r"[A-Za-z]+", text[max(0, start - 40):start])[-3:]
    last = re.findall(r"[A-Za-z]+", surface)[-1].lower()
    return any(w.lower() in CUES for w in before) or last in TRAILING


def find_mentions(text: str, iso3: str, common: set[str] = frozenset()) -> tuple[list[dict], list[str]]:
    """Region mentions in a text, and capitalized names that matched several regions (ambiguous)."""
    idx = load_index(iso3)
    if not idx:
        return [], []
    mentions, ambiguous, taken = [], [], set()
    for start, end, surface in candidate_spans(text):
        if any(i in taken for i in range(start, end)):
            continue
        key = normalize(surface)
        if key not in idx["index"] or key in SKIP or (" " not in key and key in common):
            continue
        one_word = len(re.findall(r"[A-Za-zÀ-ɏ]+", surface)) == 1
        following, previous = _next_word(text, end), _prev_word(text, start)
        if one_word and following[:1].isupper() and following.lower() not in FOLLOWERS:
            continue  # "Volodymyr Zelensky", "Khalil Ahmed": a person, not a place
        if one_word and previous[:1].isupper() and previous.lower() not in CUES:
            continue  # "Stoycho Tihomirov Kitaev": the end of a person's name
        entry = resolve(idx["index"][key])
        stated = _region_after(text, end, idx)
        if stated and entry is not None and entry["kind"] == "place" and entry["pop"] == 0 and entry["region"] != stated:
            # A small place GeoNames files elsewhere ("Dibab, West Kordofan"): trust the report. Known towns keep their region.
            entry = {**entry, "region": stated}
        elif stated and entry is None:
            entry = {"kind": "place", "region": stated, "name": surface, "pop": 0}
        if entry is not None and entry["kind"] == "place" and entry["pop"] == 0 and not _has_cue(text, start, surface):
            continue
        taken.update(range(start, end))
        if entry is None:
            if _has_cue(text, start, surface):
                ambiguous.append(surface)
            continue
        shown = re.sub(r"^(?:[a-z]+\s+)+", "", surface).strip("-’'") or surface  # "at El Fasher" -> "El Fasher"
        mentions.append({"region": entry["region"], "place": entry["name"], "kind": entry["kind"], "surface": shown})
    return mentions, ambiguous


def summarize(iso3: str, reports: list[dict], max_reports: int = 6) -> dict:
    """Per region: how many reports mention it, the spellings used, and the newest reports. Regions only, no towns'
    coordinates. Each report counts once per region however often it names places there."""
    idx = load_index(iso3)
    if not idx:
        return {"regions": [], "source": None}
    common = common_words([r["text"] for r in reports])
    stats = {}
    for r in reports:
        mentions, _ = find_mentions(r["title"] + ". " + r["text"], iso3, common)
        for code in {m["region"] for m in mentions}:
            here = [m for m in mentions if m["region"] == code]
            s = stats.setdefault(code, {"reports": [], "mentions": 0, "spellings": Counter(), "places": Counter()})
            s["reports"].append({k: r[k] for k in ("title", "source", "date", "url")})
            s["mentions"] += len(here)
            s["spellings"].update(m["surface"] for m in here)
            s["places"].update(m["place"] for m in here if m["kind"] != "region")
    regions = []
    for code, s in stats.items():
        r = idx["regions"][code]
        # Spellings grouped by the place they resolved to, so "El Fasher, Al Fasher" show as one place.
        regions.append({
            "code": code, "name": r["name"], "lat": r["lat"], "lon": r["lon"], "disputed": bool(r.get("disputed")),
            "reports": len(s["reports"]), "mentions": s["mentions"],
            "spellings": list({w.lower(): w for w, _ in reversed(s["spellings"].most_common())}.values())[::-1][:8],
            "places": [p for p, _ in s["places"].most_common(6)],
            "latest": sorted(s["reports"], key=lambda x: x["date"], reverse=True)[:max_reports],
        })
    regions.sort(key=lambda x: -x["reports"])
    return {"regions": regions, "source": idx["source"]}
