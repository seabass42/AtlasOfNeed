# Checks whether AI briefing bullets are supported by the passages they cite.
# Generates briefings with IBM Bob (default + a few questions per country), pairs every bullet with the exact passages it
# cites, and runs an automatic check: every number in the bullet must appear in those passages. The full pairs are saved
# so a person can read each bullet against its sources and judge support (the automatic check can't judge wording).
#   python scripts/eval_citations.py [out.json]       # needs BOB_API_KEY (from .env.local)
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env.local")
import briefing  # noqa: E402

QUESTIONS = ["", "How are displaced people doing?", "Is there famine or food shortage?", "What is the state of health care?"]
COUNTRIES = {"SDN": "Sudan", "UKR": "Ukraine"}


def numbers(text: str) -> list[str]:
    """Figures in a sentence, normalised: "2,055" -> "2055", "19.5" stays, years included."""
    return [n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def number_check(bullet: str, passages: list[str]) -> tuple[list[str], list[str]]:
    source = " ".join(passages).replace(",", "")
    found = numbers(bullet)
    return found, [n for n in found if n not in source]


def main(out_path: str):
    results, totals = [], {"bullets": 0, "with_numbers": 0, "numbers_ok": 0}
    for iso3, country in COUNTRIES.items():
        reports = briefing.load_reports(iso3)
        all_passages = [p for r in reports for p in briefing.chunk(r)]
        for q in QUESTIONS:
            passages = briefing.retrieve(all_passages, q, country)
            if not passages:
                continue
            ans = briefing.validate(briefing.extract_answer(briefing.run_bob(briefing.build_prompt(country, q, passages))), len(passages))
            for b in ans["bullets"]:
                cited = [passages[n - 1] for n in b["cites"]]
                nums, missing = number_check(b["text"], [p["text"] for p in cited])
                totals["bullets"] += 1
                totals["with_numbers"] += bool(nums)
                totals["numbers_ok"] += bool(nums) and not missing
                results.append({"country": country, "question": q or "(default briefing)", "bullet": b["text"], "cites": b["cites"],
                                "numbers": nums, "numbers_missing": missing,
                                "sources": [{"source": p["source"], "date": p["date"], "url": p["url"], "text": p["text"]} for p in cited]})
            print(f"{country:8} {q or '(default briefing)':38} {len(ans['bullets'])} bullets")
    Path(out_path).write_text(json.dumps({"totals": totals, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n{totals['bullets']} bullets; {totals['with_numbers']} contain figures, of which {totals['numbers_ok']} have every "
          f"figure found in the cited passages. Saved to {out_path} for reading.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "citation_check.json")
