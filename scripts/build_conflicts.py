# Builds static/conflicts.json (conflict details shown on the map) from two UCDP datasets:
#   1. UCDP/PRIO Armed Conflict Dataset: final, yearly figures (latest year: 2025)
#   2. UCDP Candidate Events Dataset: preliminary, monthly event data for the current year (2026 so far)
# We take UCDP's definitions as-is instead of hand-picking "countries at war":
#   - a state-based armed conflict involves at least one government and has 25+ battle-related deaths in a year
#   - intensity 1 = minor (25-999 deaths), intensity 2 = war (1,000+ deaths)
#   - a conflict counts toward every country in its "gwno_loc" (location) field
# Run: python scripts/build_conflicts.py
import csv
import json
import re
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_CSV = ROOT / "data" / "UcdpPrioConflict_v26_1.csv"
OUT = ROOT / "static" / "conflicts.json"
YEAR = "2025"  # latest year covered by v26.1
# Preliminary 2026 events, Jan 1 to early Oct. UCDP adds a new file each month: update the name from
# https://ucdp.uu.se/downloads/#candidate and rerun. The 13 MB file is downloaded on first run and git-ignored.
CANDIDATE_FILE = "GEDEvent_v26_01_26_09.csv"
CANDIDATE_URL = f"https://ucdp.uu.se/downloads/candidateged/{CANDIDATE_FILE}"
CANDIDATE_CSV = ROOT / "data" / CANDIDATE_FILE
MIN_DEATHS = 25    # UCDP's threshold for counting an armed conflict in a year
# Current-year totals count deaths where the fighting happened (each event's location), not by nationality: Russian
# soldiers killed in Ukraine count toward Ukraine. UCDP files events in Gaza and the West Bank under Israel's country
# code; their first-level area (adm_1) says where they happened, so they're counted for Palestine (map id 275).
TERRITORIES = {"Gaza Strip": "275", "West Bank": "275"}
WAR_DEATHS = 1000  # UCDP's threshold for "war"

# UCDP uses Gleditsch-Ward country codes; the map uses ISO 3166-1 numeric ids.
# Only codes that appear in the chosen year are listed; the script fails loudly on any it doesn't know.
GW_TO_ISO = {
    "2": "840", "41": "332", "100": "170", "200": "826", "365": "643", "369": "804",
    "432": "466", "434": "204", "436": "562", "439": "854", "461": "768", "471": "120",
    "475": "566", "482": "140", "490": "180", "501": "404", "517": "646", "520": "706",
    "530": "231", "540": "024", "541": "508", "625": "729", "626": "728", "630": "364",
    "640": "792", "645": "368", "652": "760", "666": "376", "678": "887", "700": "004",
    "750": "356", "770": "586", "775": "104", "800": "764", "811": "116", "840": "608",
    "850": "360",
}

TYPES = {"1": "Extrasystemic", "2": "Interstate", "3": "Intrastate", "4": "Internationalized intrastate"}
INCOMPATIBILITY = {"1": "Territory", "2": "Government", "3": "Government and territory"}

# UCDP keeps former names in parentheses, e.g. "Russia (Soviet Union)", "Myanmar (Burma)", "DR Congo (Zaire)".
# They read as wrong on a current map, so drop them for display. Also spell out actors whose short name means nothing to
# a reader (from the UCDP actor list, ucdp-actor-261).
FORMER_NAME = re.compile(r" \((Soviet Union|Burma|Zaire|Kampuchea|North Yemen)\)")


ACTOR_NAMES = {"SFA": "SFA (Sudan Founding Alliance, which includes the RSF)"}


def clean(name):
    return FORMER_NAME.sub("", name)


def spell_out(side):
    return ", ".join(ACTOR_NAMES.get(part, part) for part in side.split(", "))


def current_year_conflicts():
    """State-based conflicts with 25+ battle-related deaths so far this year, per map country id."""
    if not CANDIDATE_CSV.exists():
        print(f"Downloading {CANDIDATE_URL}")
        urllib.request.urlretrieve(CANDIDATE_URL, CANDIDATE_CSV)
    with CANDIDATE_CSV.open(encoding="utf-8-sig", newline="") as f:
        events = [e for e in csv.DictReader(f) if e["type_of_violence"] == "1"]  # 1 = state-based
    through = max(e["date_end"][:10] for e in events)
    totals = defaultdict(lambda: {"deaths": 0, "events": 0})
    for e in events:
        iso = TERRITORIES.get(e["adm_1"]) or GW_TO_ISO.get(e["country_id"])
        if iso:  # countries outside GW_TO_ISO had no conflict in 2025 and aren't highlighted
            t = totals[(iso, e["conflict_new_id"])]
            t["deaths"] += int(e["best"] or 0)
            t["events"] += 1
            t["sides"] = f"{clean(e['side_a'])} vs {spell_out(clean(e['side_b']))}"
    countries = defaultdict(list)
    for (iso, _), t in totals.items():
        if t["deaths"] >= MIN_DEATHS:
            countries[iso].append({**t, "intensity": 2 if t["deaths"] >= WAR_DEATHS else 1})
    out = {}
    for iso, conflicts in countries.items():
        conflicts.sort(key=lambda c: -c["deaths"])
        out[iso] = {"intensity": max(c["intensity"] for c in conflicts), "deaths": sum(c["deaths"] for c in conflicts),
                    "conflicts": conflicts}
        if iso in TERRITORIES.values():
            out[iso]["note"] = ("Counted from events located in " + " and ".join(k for k, v in TERRITORIES.items() if v == iso)
                                + ". UCDP records this conflict under Israel.")
    return out, through


def main():
    with SOURCE_CSV.open(encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["year"] == YEAR]

    countries = {}
    for r in rows:
        conflict = {
            "id": r["conflict_id"],
            "sides": f"{clean(r['side_a'])} vs {clean(r['side_b'])}",
            "intensity": int(r["intensity_level"]),
            "type": TYPES[r["type_of_conflict"]],
            "incompatibility": INCOMPATIBILITY[r["incompatibility"]],
            # Territory names in interstate conflicts (e.g. "Crimea", "Kashmir") are disputed claims; don't show them.
            "territory": (r["territory_name"] or None) if r["type_of_conflict"] != "2" else None,
            "location": clean(r["location"]),
        }
        for gw in (code.strip() for code in r["gwno_loc"].split(",")):
            if gw not in GW_TO_ISO:
                raise SystemExit(f"Unknown Gleditsch-Ward code {gw} ({r['location']}); add it to GW_TO_ISO")
            entry = countries.setdefault(GW_TO_ISO[gw], {"intensity": 0, "conflicts": []})
            entry["conflicts"].append(conflict)
            entry["intensity"] = max(entry["intensity"], conflict["intensity"])

    for entry in countries.values():
        entry["conflicts"].sort(key=lambda c: (-c["intensity"], c["sides"]))

    current, through = current_year_conflicts()
    for iso, entry in current.items():
        countries.setdefault(iso, {"intensity": 0, "conflicts": []})["current"] = entry

    out = {
        "source": {
            "name": "UCDP/PRIO Armed Conflict Dataset",
            "version": rows[0]["version"],
            "year": int(YEAR),
            "url": "https://ucdp.uu.se/downloads/",
            "definition": "State-based armed conflicts with at least 25 battle-related deaths in the year. "
                          "Minor: 25-999 deaths. War: 1,000+ deaths. Excludes non-state and one-sided violence.",
            "citation": "Gleditsch, Nils Petter, et al. (2002). Armed Conflict 1946-2001: A New Dataset. "
                        "Journal of Peace Research 39(5).",
        },
        "current_source": {
            "name": "UCDP Candidate Events Dataset",
            "version": CANDIDATE_FILE.removeprefix("GEDEvent_").removesuffix(".csv").replace("_", "."),
            "from": through[:4] + "-01-01",
            "through": through,
            "url": "https://ucdp.uu.se/downloads/#candidate",
            "definition": "Preliminary event data, released monthly and revised before the final yearly dataset. "
                          "Deaths are UCDP's best estimate of battle-related deaths in state-based conflict.",
        },
        "countries": dict(sorted(countries.items())),
    }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    wars = sum(1 for c in countries.values() if c["intensity"] == 2)
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(rows)} conflicts across {len(countries)} countries ({wars} at war level) in {YEAR}; "
          f"{len(current)} countries with active conflicts in {through[:4]} through {through}")


if __name__ == "__main__":
    main()
