# Asks IBM Bob to place the names the gazetteer couldn't: names that exist in several regions ("Tawila") and names
# GeoNames doesn't list ("Kyselivka"). Bob gets each name with the sentence it appeared in and the list of regions,
# and answers with a region code or "not a place in this country". Answers go to data/places/aliases_<iso3>.json,
# marked reviewed: false so a person can check them; names already in that file are not asked again.
#   python scripts/resolve_places.py           # needs BOB_API_KEY (from .env.local)
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env.local")
import briefing  # noqa: E402
import places  # noqa: E402

MAX_NAMES = 40
NAME = r"[A-Z][a-zà-ɏ’'-]+(?:\s+[A-Z][a-zà-ɏ’'-]+){0,2}"
CUE_NAME = re.compile(r"\b(?:in|near|at|from|outside|around|towards|across|reached|to)\s+(?:the\s+(?:town|village|city|camp|locality)\s+of\s+)?(" + NAME + ")")
LIST_START = re.compile(r"\b(?:settlements|villages|towns|localities|camps|communities|areas)\s*(?::|of|in|such as|including)\s*")
NOT_PLACES = set("""january february march april may june july august september october november december monday tuesday
    wednesday thursday friday saturday sunday government council assessment women international society societies committee
    programme program office forces mission fund cluster organization organisation nations union agency team alliance
    ministry department project initiative platform group army police""".split())


def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", text.replace("\n\n", " "))


def _is_candidate(name: str, idx: dict, common: set) -> bool:
    key = places.normalize(name)
    words = set(key.split())
    known = key in idx["index"] and places.resolve(idx["index"][key]) is not None  # ambiguous names still need help
    return (len(key) >= places.MIN_LEN and not known and key not in places.SKIP and key not in common
            and "’s" not in name and "'s" not in name and not words & NOT_PLACES)


def unresolved(iso3: str) -> dict[str, str]:
    """name -> one sentence of context, for ambiguous names and unknown names right after a location cue
    ("in Tawila") or in a list of places ("settlements: Kyselivka, Posad-Pokrovske and Blahodatne")."""
    reports = briefing.load_archive(iso3)
    common = places.common_words([r["text"] for r in reports])
    idx = places.load_index(iso3)
    found: dict[str, str] = {}
    for r in reports:
        text = r["title"] + ". " + r["text"]
        for name in places.find_mentions(text, iso3, common)[1]:
            found.setdefault(name, next((s for s in sentences(text) if name in s), ""))
        for sent in sentences(text):
            names = [m.group(1) for m in CUE_NAME.finditer(sent)]
            for m in LIST_START.finditer(sent):
                items = re.split(r",\s*|\s+and\s+", re.split(r"[.;(]", sent[m.end():])[0])
                names += [i.strip() for i in items if re.fullmatch(NAME, i.strip())]
            for name in names:
                if _is_candidate(name, idx, common):
                    found.setdefault(name, sent)
    return found


def ask_bob(country: str, regions: dict, names: dict[str, str]) -> list[dict]:
    region_list = "\n".join(f"{code}: {r['name']}" + (" (disputed area)" if r.get("disputed") else "") for code, r in regions.items())
    items = "\n".join(f'- "{n}" | context: {ctx[:300]}' for n, ctx in names.items())
    prompt = f"""You are matching place names found in humanitarian reports about {country} to the region they are in.
For each name, decide from the context and your general geographic knowledge whether it is a place (town, village, camp,
locality) located in {country}. If it is, give the code of the region it is in, from this list:
{region_list}
If it is not a place in {country} (a person, organisation, ethnic group, or a place in another country), or you are not
sure, use null. Do not guess. Also give the usual English spelling as "canonical".
Return ONLY valid JSON, no other text: a list like
[{{"name": "Tawila", "region": "55", "canonical": "Tawila"}}, {{"name": "Musa", "region": null, "canonical": null}}]

NAMES:
{items}"""
    return _parse_list(briefing.run_bob(prompt))


def _parse_list(stdout: str) -> list[dict]:
    """Bob's last_message holds a JSON list; extract_answer expects an object, so parse the list here."""
    text = stdout
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "error":
            raise RuntimeError(event.get("message", "Bob error"))
        if isinstance(event, dict) and isinstance(event.get("last_message"), str):
            text = event["last_message"]
    start = text.find("[")
    answer, _ = json.JSONDecoder().raw_decode(text[start:])
    return [a for a in answer if isinstance(a, dict) and isinstance(a.get("name"), str)]


def main():
    for iso3, country in (("SDN", "Sudan"), ("UKR", "Ukraine")):
        alias_file = places.PLACES_DIR / f"aliases_{iso3.lower()}.json"
        existing = json.loads(alias_file.read_text(encoding="utf-8")) if alias_file.exists() else []
        asked = {places.normalize(a["name"]) for a in existing}
        names = {n: c for n, c in unresolved(iso3).items() if places.normalize(n) not in asked}
        names = dict(list(names.items())[:MAX_NAMES])
        if not names:
            print(f"{iso3}: nothing new to resolve")
            continue
        regions = places.load_index(iso3)["regions"]
        print(f"{iso3}: asking Bob about {len(names)} names...")
        answers = ask_bob(country, regions, names)
        today = date.today().isoformat()
        new = []
        for a in answers:
            if a["name"] not in names:
                continue
            region = a.get("region") if a.get("region") in regions else None
            new.append({"name": a["name"], "region": region, "canonical": a.get("canonical") if region else None,
                        "context": names[a["name"]][:300], "added_by": "IBM Bob", "added": today, "reviewed": False})
            label = f"{regions[region]['name']} ({a.get('canonical')})" if region else "not a place here"
            print(f"   {a['name']:28} -> {label}")
        alias_file.write_text(json.dumps(existing + new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"   saved to {alias_file.relative_to(ROOT)}: review it and set reviewed: true for answers you've checked")


if __name__ == "__main__":
    main()
