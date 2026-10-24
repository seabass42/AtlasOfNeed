# Builds static/conflicts.json (which countries the map highlights) from the UCDP/PRIO Armed Conflict Dataset.
# We take UCDP's definitions as-is instead of hand-picking "countries at war":
#   - a state-based armed conflict involves at least one government and has 25+ battle-related deaths in a year
#   - intensity 1 = minor (25-999 deaths), intensity 2 = war (1,000+ deaths)
#   - a conflict counts toward every country in its "gwno_loc" (location) field
# Run: python scripts/build_conflicts.py
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_CSV = ROOT / "data" / "UcdpPrioConflict_v26_1.csv"
OUT = ROOT / "static" / "conflicts.json"
YEAR = "2025"  # latest year covered by v26.1

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
# They read as wrong on a current map, so drop them for display.
FORMER_NAME = re.compile(r" \((Soviet Union|Burma|Zaire|Kampuchea|North Yemen)\)")


def clean(name):
    return FORMER_NAME.sub("", name)


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
        "countries": dict(sorted(countries.items())),
    }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    wars = sum(1 for c in countries.values() if c["intensity"] == 2)
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(rows)} conflicts across {len(countries)} countries ({wars} at war level)")


if __name__ == "__main__":
    main()
