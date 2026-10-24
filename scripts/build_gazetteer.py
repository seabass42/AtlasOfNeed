# Builds data/places/<iso3>.json, the place-name list used to find which regions reports talk about.
# Source: GeoNames (https://www.geonames.org, CC BY 4.0). Keeps, per country:
#   - regions (states / oblasts) with one representative point each, which is where the map draws a bubble
#   - populated places with their region and Latin-script spelling variants ("El Fasher", "Al-Fashir", "Al Fashir"...)
# Towns get no coordinates at all: the map only ever shows regions.
#   python scripts/build_gazetteer.py
import csv
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "places"
GEONAMES = "https://download.geonames.org/export/dump/{}.zip"
COLUMNS = "geonameid name asciiname alternatenames lat lon fclass fcode cc cc2 a1 a2 a3 a4 pop elev dem tz mod".split()
COUNTRIES = {
    # iso3: (GeoNames country code, minimum population for towns). Sudan's population data is sparse (Tawila, a camp
    # town of hundreds of thousands, is listed with 0), so it keeps every populated place; Ukraine has tens of
    # thousands of small villages with repeated names, so it keeps places of 1,000+.
    "SDN": ("SD", 0),
    "UKR": ("UA", 1000),
}
# Areas whose status is disputed get their own entry instead of being counted under a state, so the map takes no
# position on sovereignty. GeoNames files Abyei under West Kordofan.
DISPUTED = {"SDN": {"Abyei": "Abyei Area (disputed)"}}
# Usual English names where GeoNames' region names are inconsistent ("Sumska", "Misto Kyiv"). Their original names
# stay in the variants, so matching is unaffected.
DISPLAY_NAMES = {
    "UKR": {"Misto Kyiv": "Kyiv city", "Sebastopol City": "Sevastopol", "Donetska": "Donetsk", "Luhanska": "Luhansk",
            "Lvivska": "Lviv", "Odeska": "Odesa", "Sumska": "Sumy", "Volynska": "Volyn", "Mykolayiv": "Mykolaiv",
            "Zaporizhzhya": "Zaporizhzhia", "Vinnytsya": "Vinnytsia", "Autonomous Republic of Crimea": "Crimea"},
    "SDN": {"Al Jazirah": "Al Jazirah (Gezira)", "Al Qadarif": "Gedaref"},
}


def latin_variants(row: dict) -> list[str]:
    names = [row["name"], row["asciiname"]] + row["alternatenames"].split(",")
    keep = []
    for n in names:
        n = n.strip()
        if not n or not all(ord(c) < 0x250 or c in "’ʼʻ‘" for c in n):  # Latin script only
            continue
        if n.isupper() and len(n) <= 4:  # airport and other codes, e.g. "ELF", "HRK"
            continue
        keep.append(n)
    return list(dict.fromkeys(keep))


def build(iso3: str) -> dict:
    cc, min_pop = COUNTRIES[iso3]
    with urllib.request.urlopen(GEONAMES.format(cc), timeout=60) as r:
        text = zipfile.ZipFile(io.BytesIO(r.read())).read(f"{cc}.txt").decode("utf-8")
    rows = [dict(zip(COLUMNS, line)) for line in csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)]

    regions = {}
    for r in rows:
        if r["fcode"] == "ADM1":
            name = re.sub(r" (State|Oblast)$", "", r["name"])
            name = DISPLAY_NAMES.get(iso3, {}).get(name, name)
            regions[r["a1"]] = {"name": name, "lat": round(float(r["lat"]), 2), "lon": round(float(r["lon"]), 2),
                                "variants": latin_variants(r)}
    places = []
    disputed = DISPUTED.get(iso3, {})
    for r in rows:
        if r["fclass"] != "P" or r["a1"] not in regions:
            continue
        pop = int(r["pop"] or 0)
        if r["name"] in disputed:
            key = f"X-{r['name']}"
            regions[key] = {"name": disputed[r["name"]], "lat": round(float(r["lat"]), 2), "lon": round(float(r["lon"]), 2),
                            "variants": latin_variants(r), "disputed": True}
            continue
        if pop < min_pop and r["fcode"] not in ("PPLC", "PPLA", "PPLA2"):  # always keep capitals and admin seats
            continue
        places.append({"name": r["name"], "region": r["a1"], "pop": pop, "variants": latin_variants(r)})
    places.sort(key=lambda p: -p["pop"])
    return {
        "source": "GeoNames (geonames.org), CC BY 4.0",
        "note": "Regions have one representative point each. Towns have no coordinates: the map only shows regions.",
        "regions": dict(sorted(regions.items())),
        "places": places,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for iso3 in COUNTRIES:
        g = build(iso3)
        out = OUT_DIR / f"{iso3.lower()}.json"
        out.write_text(json.dumps(g, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"{iso3}: {len(g['regions'])} regions, {len(g['places'])} places -> {out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
