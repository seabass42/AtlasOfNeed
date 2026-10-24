"""RAG briefing for Atlas of Need.

1. Load saved reports for a country (data/reports/<iso3>.json)
2. Split them into passages and pick the most relevant ones (embedding similarity + recency; keyword match if the
   embedding model can't be loaded)
3. Ask IBM Bob (`bob run`) to summarize ONLY those passages, citing passage numbers
4. Validate: drop any sentence without a real citation
5. If Bob is missing, slow, or returns bad output, fall back to source excerpts

build_update() drafts a one-page weekly situation update for an NGO or diaspora organisation to send to donors or
members: key figures from the verified profiles, plus sections written from this week's reports, every sentence cited.

build_changes() does the same for "What's new": it compares reports from the last 7 days in data/archive/ with
earlier ones and asks Bob what is new (new displacement figures, newly affected areas, access restrictions...).
"""
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
REPORTS_DIR = DATA_DIR / "reports"
ARCHIVE_DIR = DATA_DIR / "archive"  # every report ever fetched or added, for comparing weeks (see scripts/archive.py)
PROFILES_DIR = DATA_DIR / "profiles"  # verified key figures with source quotes (see scripts/verify_profiles.py)
MAX_PASSAGES = 8
MAX_PER_REPORT = 2  # so one long report can't fill every slot with near-duplicate passages
BOB_TIMEOUT_S = 60
# The briefing only needs the passages in the prompt, so Bob gets no tools (files, commands, MCP, subagents...).
BOB_TOOL_GROUPS_OFF = "read,edit,execute,mcp,skill,todo,subagent,mode"

# Semantic search: passages and questions become vectors, and similar meaning means nearby vectors, so "refugees" finds
# "families fled their homes" without sharing a word. Small English model (~67 MB), downloaded once into .cache/.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
CACHE_DIR = Path(__file__).parent / ".cache"
# Calibrated on the October 2026 reports: off-topic questions ("what is 1+1", "best pizza recipe") scored at most 0.58,
# questions the reports cover scored 0.66 or more.
MIN_SIMILARITY = 0.62       # below this, the reports don't answer the question at all
MIN_PASSAGE_SIMILARITY = 0.55  # weaker passages are left out even when the question does match
RECENCY_WEIGHT = 0.02       # small nudge toward newer reports when similarity is close
DEFAULT_QUERY = ("What is the current humanitarian situation: displacement, food insecurity, health, "
                 "civilian casualties, humanitarian access and funding needs?")
CHANGES_QUERY = ("What happened recently: attacks and people killed, places newly affected by fighting, people newly "
                 "displaced, restrictions on humanitarian access, and new food, health or funding figures?")
BASELINE_DAYS = 45      # "earlier" reports for What's new: from 45 days ago up to the start of the window
MAX_NEW = 12            # new passages shown to Bob, one per report so more of the week's reports are covered
MAX_EARLIER = 6         # earlier passages shown to Bob for comparison
CHANGE_CATEGORIES = ("displacement", "areas", "access", "needs", "other")

# Keyword fallback, only used when the embedding model is unavailable.
STOP = set("the a an and or of to in on for with is are was were be by at from as that this it its into than then".split())
QUESTION_STOP = set("""what who whom whose when where why how which will would could should can does did doing done
    has have had about tell please there their they them any much many some more most also just like know give show
    explain describe happening going right now today currently current latest recent news update info information""".split())


def load_conflicts() -> dict:
    return json.loads((DATA_DIR / "conflicts.json").read_text(encoding="utf-8"))


def load_archive(iso3: str) -> list[dict]:
    f = ARCHIVE_DIR / f"{iso3.lower()}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def load_profile(iso3: str) -> dict | None:
    f = PROFILES_DIR / f"{iso3.lower()}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def load_reports(iso3: str) -> list[dict]:
    f = REPORTS_DIR / f"{iso3.lower()}.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def chunk(report: dict) -> list[dict]:
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", report.get("text", ""))]
    paras = [p for p in paras if len(p) > 40]
    out, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) > 700:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf} {p}".strip()
    if buf:
        out.append(buf)
    meta = {k: report.get(k, "") for k in ("title", "source", "date", "url")}
    return [{"text": t, **meta} for t in out]


def tokens(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-zà-ɏ]{3,}", s.lower()) if w not in STOP]


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return date.min


_model = None
_model_lock = threading.Lock()
_vectors: dict = {}


def embedding_model():
    """The embedding model, loaded once. None if fastembed isn't installed or the model can't be downloaded."""
    global _model
    with _model_lock:
        if _model is None:
            try:
                from fastembed import TextEmbedding
                _model = TextEmbedding(EMBED_MODEL, cache_dir=str(CACHE_DIR / "models"))
            except Exception as e:  # missing package, no internet on first run...
                print("Embeddings unavailable, using keyword search:", str(e)[:200])
                _model = False
        return _model or None


def _unit(vectors):
    import numpy as np
    vectors = np.asarray(vectors, dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=-1, keepdims=True)


def passage_vectors(passages: list[dict]):
    """Embeddings for these passages, cached in memory and in .cache/embeddings/ (keyed by the passage text)."""
    import numpy as np
    key = hashlib.sha256("\x00".join(p["text"] for p in passages).encode()).hexdigest()[:16]
    if key not in _vectors:
        f = CACHE_DIR / "embeddings" / f"{key}.npy"
        if f.exists():
            vectors = np.load(f)
        else:
            vectors = _unit(list(embedding_model().passage_embed([p["text"] for p in passages])))
            f.parent.mkdir(parents=True, exist_ok=True)
            np.save(f, vectors)
        _vectors[key] = vectors
    return _vectors[key]


def warm_up() -> None:
    """Load the model and embed every briefing country's reports, so the first click doesn't wait for it."""
    if embedding_model() is None:
        return
    for c in load_conflicts()["countries"]:
        new_reports, earlier_reports, _, _ = change_sets(c["iso3"], 7, date.today())
        for reports in (load_reports(c["iso3"]), new_reports, earlier_reports):
            if reports:
                passage_vectors([p for r in reports for p in chunk(r)])
    print("Embeddings ready")


def _freshness(passages: list[dict]) -> list[float]:
    """1.0 for the newest report, falling to 0 at 90 days older."""
    newest = max(_parse_date(p.get("date", "")) for p in passages)
    if newest == date.min:
        return [0.0] * len(passages)
    return [max(0.0, 1 - (newest - _parse_date(p.get("date", ""))).days / 90) for p in passages]


def _cap_per_report(ranked: list[dict], limit: int = MAX_PASSAGES, per_report: int = MAX_PER_REPORT) -> list[dict]:
    picked, counts = [], {}
    for p in ranked:
        key = p.get("url") or p.get("title")
        if counts.get(key, 0) < per_report:
            counts[key] = counts.get(key, 0) + 1
            picked.append(p)
        if len(picked) == limit:
            break
    return picked


def retrieve(passages: list[dict], question: str = "", country: str = "", default_query: str = DEFAULT_QUERY,
             limit: int = MAX_PASSAGES, per_report: int = MAX_PER_REPORT) -> list[dict]:
    """Most relevant passages, at most MAX_PER_REPORT per report.

    Without a question, ranks against a default needs-focused query. With a question, returns [] when nothing in the
    reports is close enough to answer it, so an off-topic question gets no passages instead of random ones.
    """
    if not passages:
        return []
    model = embedding_model()
    if model is None:
        return _cap_per_report(keyword_retrieve(passages, question, country, default_query, limit=len(passages)), limit, per_report)
    query = _unit(list(model.query_embed([question or default_query])))[0]
    similarity = passage_vectors(passages) @ query
    if question and similarity.max() < MIN_SIMILARITY:
        return []
    scores = [sim + RECENCY_WEIGHT * fresh for sim, fresh in zip(similarity.tolist(), _freshness(passages))]
    order = sorted(range(len(passages)), key=lambda i: scores[i], reverse=True)
    return _cap_per_report([passages[i] for i in order if not question or similarity[i] >= MIN_PASSAGE_SIMILARITY], limit, per_report)


def keyword_retrieve(passages: list[dict], question: str = "", country: str = "", default_query: str = DEFAULT_QUERY,
                     limit: int = MAX_PASSAGES) -> list[dict]:
    """Fallback: rank by shared words. Filler words and the country's own name don't count as a match."""
    skip = QUESTION_STOP | set(tokens(country))
    q = {w[:6] for w in tokens(question or default_query) if not question or w not in skip}  # w[:6]: crude stem
    if not q:
        return []

    def hits(p):
        return sum(1 for w in tokens(p["text"]) if w[:6] in q)

    scored = [(hits(p) / math.sqrt(len(tokens(p["text"])) + 1) + 0.3 * fresh, p)
              for p, fresh in zip(passages, _freshness(passages)) if not question or hits(p) > 0]
    return _cap_per_report([p for _, p in sorted(scored, key=lambda x: x[0], reverse=True)], limit)


def build_prompt(country: str, question: str, passages: list[dict]) -> str:
    numbered = "\n\n".join(f"[{i + 1}] ({p['source']}, {p['date']}) {p['text']}" for i, p in enumerate(passages))
    if question:
        focus = (f"The reader asked: {question}\n"
                 "Answer ONLY that question. If the passages do not contain information that answers it, return an empty "
                 "bullets list and use gaps to say plainly that the sources do not cover it. Never fill the answer with "
                 "unrelated facts from the passages.\n")
        length = "Write up to 6 bullets that directly answer the question, or none if the passages don't answer it."
    else:
        focus = ""
        length = "Write 4 to 6 bullets."
    return f"""You are writing a short humanitarian situation briefing about {country} for journalists, students and NGO staff.
Use ONLY the numbered passages below. Do not use any tools and do not use outside knowledge. Do not predict events, give military analysis,
take sides, or give safety or evacuation advice. If the passages do not cover something, say so.
{focus}
Return ONLY valid JSON, no other text, in this exact shape:
{{"bullets":[{{"text":"one factual sentence","cites":[1,2]}}],"gaps":"one sentence on what the sources do not cover"}}
{length} Every bullet must cite at least one passage number.

PASSAGES:
{numbered}"""


def run_bob(prompt: str) -> str:
    # Run in an empty temp folder so Bob cannot read or change project files.
    with tempfile.TemporaryDirectory(prefix="bob-") as cwd:
        r = subprocess.run(
            ["bob", "run", "--mode", "ask", "--max-turns", "1", "--format", "json",
             "--disable-tool-groups", BOB_TOOL_GROUPS_OFF, "--disable-mcp", "--disable-subagents", prompt],
            # stdin=DEVNULL: bob reads piped stdin as extra input and would otherwise wait on the server's stdin.
            cwd=cwd, env=os.environ.copy(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=BOB_TIMEOUT_S,
        )
    if r.returncode != 0:
        raise RuntimeError(r.stderr or f"bob exited {r.returncode}")
    return r.stdout


def extract_answer(stdout: str) -> dict:
    """Find the model's text in Bob's output, then the JSON object inside it.

    Bob 2.x prints one JSON object per line: optional {"type":"error",...} lines, then {"type":"result","last_message":...}.
    Older versions used other key names, so those are still accepted.
    """
    text = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            raise ValueError(f"Bob error: {event.get('message', '')}")
        found = next((event[k] for k in ("last_message", "result", "response", "output", "text", "message")
                      if isinstance(event.get(k), str)), None)
        if found is not None:
            text = found
    if text is None:
        text = stdout  # not JSON lines: look for the answer in the raw output
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON in Bob output")
    answer, _ = json.JSONDecoder().raw_decode(text[start:])  # ignores any text after the object, e.g. a closing ``` fence
    return answer


def validate(ans: dict, n: int) -> dict:
    if not isinstance(ans, dict) or not isinstance(ans.get("bullets"), list):
        raise ValueError("No bullets")
    bullets = []
    for b in ans["bullets"]:
        if not isinstance(b, dict) or not isinstance(b.get("text"), str) or not b["text"].strip():
            continue
        cites = []
        for c in b.get("cites") or []:
            try:
                c = int(c)
            except (TypeError, ValueError):
                continue
            if 1 <= c <= n and c not in cites:
                cites.append(c)
        if cites:  # drop anything uncited
            bullets.append({"text": b["text"].strip()[:400], "cites": cites})
    gaps = ans.get("gaps") if isinstance(ans.get("gaps"), str) else ""
    if not ans["bullets"] and gaps.strip():
        return {"bullets": [], "gaps": gaps[:300], "answered": False}  # the model says the sources don't cover it
    if not bullets:
        raise ValueError("No cited bullets")
    return {"bullets": bullets[:6], "gaps": gaps[:300], "answered": True}


def fallback(passages: list[dict]) -> dict:
    bullets, seen = [], set()
    for i, p in enumerate(passages):
        if p.get("url") in seen:  # one excerpt per report
            continue
        seen.add(p.get("url"))
        bullets.append({"text": re.split(r"(?<=[.!?])\s", p["text"])[0][:300], "cites": [i + 1]})
        if len(bullets) == 4:
            break
    return {"bullets": bullets, "answered": True,
            "gaps": "AI summary unavailable; showing the opening sentence of the most relevant source passages."}


def _base(iso3: str) -> dict:
    meta = next((c for c in load_conflicts()["countries"] if c["iso3"] == iso3), None)
    return {
        "iso3": iso3,
        "country": meta["name"] if meta else iso3,
        "links": meta.get("officialLinks", []) if meta else [],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def build_briefing(iso3: str, question: str = "") -> dict:
    base = _base(iso3)
    reports = load_reports(iso3)
    if not reports:
        return {**base, "mode": "none", "answered": False, "bullets": [], "gaps": "No briefing has been prepared for this country yet.", "sources": []}

    passages = retrieve([p for r in reports for p in chunk(r)], question, base["country"])
    if not passages:  # only possible with a question: nothing in the reports matches it
        return {**base, "mode": "no_match", "answered": False, "bullets": [], "sources": [],
                "gaps": f"The saved reports about {base['country']} don't mention anything matching this question, "
                        "so there is nothing to answer it with. Try asking about displacement, food, health, "
                        "shelter or humanitarian access."}
    sources = [{"n": i + 1, "title": p["title"], "source": p["source"], "date": p["date"], "url": p["url"]} for i, p in enumerate(passages)]
    newest = max((s["date"] for s in sources), default="")

    if os.environ.get("BOB_API_KEY"):
        try:
            out = run_bob(build_prompt(base["country"], question, passages))
            return {**base, "mode": "ai", "newestSource": newest, **validate(extract_answer(out), len(passages)), "sources": sources}
        except Exception as e:  # any failure -> safe fallback
            print("Bob failed, using fallback:", str(e)[:200])
    return {**base, "mode": "fallback", "newestSource": newest, **fallback(passages), "sources": sources}


# --- What's new: compare the last few days with earlier weeks ------------------------------------------------------

def earlier_matches(new: list[dict], earlier: list[dict]) -> list[dict]:
    """Earlier passages most similar to the selected new ones, i.e. the ones a "new" fact might update."""
    if not earlier:
        return []
    if embedding_model() is None:
        return retrieve(earlier, default_query=CHANGES_QUERY, limit=MAX_EARLIER)
    similarity = passage_vectors(new) @ passage_vectors(earlier).T  # rows: new passages, columns: earlier passages
    best = {}
    for row in similarity:
        for j in row.argsort()[::-1][:2]:  # each new passage's two closest earlier passages
            best[int(j)] = max(best.get(int(j), 0.0), float(row[j]))
    ranked = [earlier[j] for j in sorted(best, key=best.get, reverse=True) if best[j] >= MIN_PASSAGE_SIMILARITY]
    return _cap_per_report(ranked, MAX_EARLIER)


def build_changes_prompt(country: str, window: dict, baseline: dict, new: list[dict], earlier: list[dict]) -> str:
    numbered = "\n\n".join(
        f"[{i + 1}] {label} ({p['source']}, {p['date']}) {p['text']}"
        for i, (label, p) in enumerate([("NEW", p) for p in new] + [("EARLIER", p) for p in earlier]))
    return f"""You are tracking how the humanitarian situation in {country} has changed, for journalists, NGO staff and people who follow the crisis every week.
Passages marked NEW come from reports published {window['from']} to {window['to']}. Passages marked EARLIER come from reports published {baseline['from']} to {baseline['to']}.
List what is NEW in the NEW passages compared with the EARLIER ones, using these categories:
- "displacement": new displacement figures or movements of people
- "areas": places newly affected by fighting or attacks
- "access": new restrictions on humanitarian access, or access reopening
- "needs": new food, health, water or funding figures
- "other": other significant new developments
What counts as new:
- Something that happened, was measured or was first reported during the NEW period. Lead with concrete events: attacks,
  places newly affected, people newly displaced, access opening or closing.
- An event or figure that a passage dates to before {window['from']} (for example "on 5 October", "as of February 2026",
  "in 2026 ... will need") is background, not a change, even when a NEW report repeats it. Leave it out, unless it
  replaces an earlier figure for exactly the same measure.
- Annual plan figures, appeals and long-standing statistics restated in a NEW report are background, not changes.
- Only compare figures that measure the same thing for the same place (internal displacement vs total displacement, or
  one town vs the whole country, are different measures). Never present different measures as an increase or decrease.
- Not new: anything the EARLIER passages already reported.
Rules: Use ONLY the passages. Do not use any tools or outside knowledge. Every item must cite at least one NEW passage.
If an item updates a figure in an EARLIER passage, give the earlier figure too and cite that EARLIER passage.
Do not speculate about causes or trends the passages don't state, predict events, give military analysis, take sides,
or give safety or evacuation advice.
Leave out categories with nothing new. If nothing is new at all, return an empty changes list.

Return ONLY valid JSON, no other text, in this exact shape:
{{"changes":[{{"category":"displacement","text":"one factual sentence","cites":[1,9]}}],"gaps":"one sentence on what the reports don't let us compare"}}
Write up to 8 items.

PASSAGES:
{numbered}"""


def validate_changes(ans: dict, n_new: int, n_total: int) -> dict:
    """Keeps items that cite at least one NEW passage; drops invalid citations and unknown categories become "other"."""
    if not isinstance(ans, dict) or not isinstance(ans.get("changes"), list):
        raise ValueError("No changes list")
    changes = []
    for c in ans["changes"]:
        if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
            continue
        cites = []
        for n in c.get("cites") or []:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= n_total and n not in cites:
                cites.append(n)
        if any(n <= n_new for n in cites):  # must rest on a new report
            category = c.get("category") if c.get("category") in CHANGE_CATEGORIES else "other"
            changes.append({"category": category, "text": c["text"].strip()[:400], "cites": cites})
    gaps = ans.get("gaps") if isinstance(ans.get("gaps"), str) else ""
    if not changes and ans["changes"]:
        raise ValueError("No change cites a new report")
    return {"changes": changes[:8], "gaps": gaps[:300], "answered": bool(changes)}


def fallback_changes(new: list[dict]) -> dict:
    """Without Bob: list the new reports themselves, one line each."""
    changes, seen = [], set()
    for i, p in enumerate(new):
        if p["url"] not in seen:
            seen.add(p["url"])
            changes.append({"category": "other", "text": f"{p['title']}: " + re.split(r"(?<=[.!?])\s", p["text"])[0][:240], "cites": [i + 1]})
    return {"changes": changes[:6], "answered": True,
            "gaps": "AI comparison unavailable; listing the most relevant reports published in this period instead."}


def change_sets(iso3: str, days: int, today: date) -> tuple[list[dict], list[dict], dict, dict]:
    """Archived reports split into the last `days` days ("new") and the BASELINE_DAYS before that ("earlier")."""
    window_start = today - timedelta(days=days - 1)
    baseline_start = today - timedelta(days=BASELINE_DAYS)
    window = {"from": window_start.isoformat(), "to": today.isoformat()}
    baseline = {"from": baseline_start.isoformat(), "to": (window_start - timedelta(days=1)).isoformat()}
    archive = load_archive(iso3)
    new_reports = [r for r in archive if window_start <= _parse_date(r["date"]) <= today]
    earlier_reports = [r for r in archive if baseline_start <= _parse_date(r["date"]) < window_start]
    return new_reports, earlier_reports, window, baseline


def build_changes(iso3: str, days: int = 7, today: date | None = None) -> dict:
    """What's new in the last `days` days compared with the weeks before, from data/archive/<iso3>.json."""
    base = _base(iso3)
    new_reports, earlier_reports, window, baseline = change_sets(iso3, days, today or date.today())
    base.update(window=window, baseline=baseline, newReports=len(new_reports), earlierReports=len(earlier_reports))
    if not new_reports:
        return {**base, "mode": "none", "answered": False, "changes": [], "sources": [],
                "gaps": f"No reports from the last {days} days are saved for {base['country']}. "
                        "Run python scripts/fetch_reports.py or add reports with scripts/add_report.py."}

    new = retrieve([p for r in new_reports for p in chunk(r)], default_query=CHANGES_QUERY, limit=MAX_NEW, per_report=1)
    earlier = earlier_matches(new, [p for r in earlier_reports for p in chunk(r)])
    sources = [{"n": i + 1, "title": p["title"], "source": p["source"], "date": p["date"], "url": p["url"], "period": period}
               for i, (period, p) in enumerate([("new", p) for p in new] + [("earlier", p) for p in earlier])]

    if os.environ.get("BOB_API_KEY"):
        try:
            out = run_bob(build_changes_prompt(base["country"], window, baseline, new, earlier))
            return {**base, "mode": "ai", **validate_changes(extract_answer(out), len(new), len(sources)), "sources": sources}
        except Exception as e:
            print("Bob failed, using fallback:", str(e)[:200])
    return {**base, "mode": "fallback", **fallback_changes(new), "sources": sources[:len(new)]}


# --- Weekly update: a one-page, sourced draft for donors or members ------------------------------------------------

UPDATE_REPORTS = 10  # this week's passages given to Bob, one per report
# Profile sections used for the "Key figures" box, in order; the first fact of each that exists, up to KEY_FIGURES.
KEY_SECTIONS = ("people_in_need", "displacement", "civilian_casualties", "food_security", "health", "humanitarian_response")
KEY_FIGURES = 5
UPDATE_SECTIONS = ("summary", "this_week", "needs", "response")
SECTION_LIMITS = {"summary": 3, "this_week": 4, "needs": 3, "response": 2}  # keeps the update to one page
WORD_BUDGET = 330  # AI-written words that fit on one A4 page next to the key figures and the compact source list
TRIM_ORDER = (("response", 1), ("needs", 1), ("this_week", 2), ("summary", 1))  # (section, fewest bullets to keep)
AUDIENCES = {
    "donors": ("a small humanitarian NGO", "its donors and supporters",
               "Emphasise humanitarian needs, the aid response and funding gaps."),
    "members": ("a diaspora community organisation", "its members",
                "Emphasise what happened this week, where, and how people are affected."),
}


def key_figures(iso3: str) -> list[dict]:
    """Verified profile facts for the Key figures box: each has its exact source quote, checked by verify_profiles.py."""
    profile = load_profile(iso3)
    if not profile:
        return []
    sections = {s["key"]: s for s in profile["sections"]}
    return [{"label": sections[k]["title"], **sections[k]["facts"][0]} for k in KEY_SECTIONS if k in sections][:KEY_FIGURES]


def build_update_prompt(country: str, audience: str, window: dict, reports: list[dict], figures: list[dict]) -> str:
    who, readers, tone = AUDIENCES[audience]
    numbered = "\n\n".join(
        [f"[{i + 1}] REPORT ({p['source']}, {p['date']}) {p['text']}" for i, p in enumerate(reports)]
        + [f"[{len(reports) + i + 1}] FIGURE ({f['source']}, {f['date']}) {f['quote']}" for i, f in enumerate(figures)])
    return f"""You are drafting a one-page weekly situation update about {country} that {who} will send to {readers}.
Write in plain, neutral language for non-specialists. {tone}
Use ONLY the numbered passages below. REPORT passages were published {window['from']} to {window['to']}. FIGURE passages are
verified key figures, each with its own date.
Rules:
- Every sentence must cite at least one passage number.
- Every number must come from a cited passage, unchanged. If a figure is from before {window['from']}, say "as of" its date.
- Do not use any tools or outside knowledge. Do not add estimates, predictions, military analysis, blame beyond what the
  passages state, safety or evacuation advice, or appeals for money.
- Only write "this week" for things a REPORT passage dates between {window['from']} and {window['to']}. Otherwise give the
  date, or say "recently".
- this_week items must cite a REPORT passage.
- FIGURE passages are printed separately in a "Key figures" box. Use them for context, but don't restate them as bullets.
- The headline has no numbers and no citations.

Return ONLY valid JSON, no other text, in this exact shape:
{{"headline":"short factual headline","summary":[{{"text":"one sentence","cites":[1]}}],"this_week":[{{"text":"...","cites":[2]}}],"needs":[{{"text":"...","cites":[3]}}],"response":[{{"text":"...","cites":[4]}}],"gaps":"one sentence on what the sources do not cover"}}
Lengths: summary 2-3 short sentences; this_week 3-4 bullets on what happened during the week; needs 2-3 bullets on
humanitarian needs; response 1-2 bullets on the aid response and funding. One sentence per bullet. Keep the whole update
under 320 words: it must fit on one page with the key figures and sources.

PASSAGES:
{numbered}"""


def validate_update(ans: dict, n: int, n_reports: int) -> dict:
    """Keeps only cited sentences in each section, up to SECTION_LIMITS. "This week" items must cite one of this week's
    reports (passages 1..n_reports). A headline with numbers is dropped, since it can't carry a citation."""
    if not isinstance(ans, dict):
        raise ValueError("Not an object")
    out = {}
    for key in UPDATE_SECTIONS:
        try:
            items = validate({"bullets": ans.get(key) if isinstance(ans.get(key), list) else []}, n)["bullets"]
        except ValueError:  # nothing cited in this section: leave it out rather than reject the whole draft
            items = []
        if key == "this_week":
            items = [b for b in items if any(c <= n_reports for c in b["cites"])]
        out[key] = items[:SECTION_LIMITS[key]]
    _trim_to_budget(out)
    if not out["summary"] and not out["this_week"]:
        raise ValueError("No cited summary or weekly items")
    headline = ans.get("headline") if isinstance(ans.get("headline"), str) else ""
    out["headline"] = headline.strip()[:120] if headline.strip() and not re.search(r"\d", headline) else ""
    out["gaps"] = ans.get("gaps")[:300] if isinstance(ans.get("gaps"), str) else ""
    return out


def _trim_to_budget(sections: dict) -> None:
    """Drops trailing bullets, least important sections first, until the draft fits WORD_BUDGET."""
    def words():
        return sum(len(b["text"].split()) for key in UPDATE_SECTIONS for b in sections[key])
    for key, keep in TRIM_ORDER:
        while words() > WORD_BUDGET and len(sections[key]) > keep:
            sections[key].pop()


def renumber_sources(update: dict) -> dict:
    """Keeps only cited sources, merges ones with the same URL, and numbers them in reading order, so the printed source
    list is short and starts at [1]."""
    by_n = {s["n"]: s for s in update["sources"]}
    in_order = ([b for b in update["summary"]] + update["keyFigures"]
                + [b for key in ("this_week", "needs", "response") for b in update[key]])
    new_n, by_url, sources = {}, {}, []
    for item in in_order:
        for n in item["cites"]:
            if n in new_n:
                continue
            src = by_n[n]
            if src["url"] not in by_url:
                by_url[src["url"]] = len(sources) + 1
                sources.append({**src, "n": len(sources) + 1})
            new_n[n] = by_url[src["url"]]
    for item in in_order:
        item["cites"] = list(dict.fromkeys(new_n[n] for n in item["cites"]))
    return {**update, "sources": sources}


def fallback_update(reports: list[dict]) -> dict:
    """Without Bob: opening sentences of this week's most relevant reports, one per report."""
    items = fallback(reports)["bullets"]
    return {"headline": "", "summary": items[:1], "this_week": items[1:5], "needs": [], "response": [],
            "gaps": "AI drafting unavailable: showing the opening sentence of this week's most relevant reports. Write the summary yourself."}


def build_update(iso3: str, audience: str = "donors", days: int = 7, today: date | None = None) -> dict:
    """One-page weekly update: verified key figures plus AI-written sections from this week's reports, all cited."""
    if audience not in AUDIENCES:
        raise ValueError(f"audience must be one of {sorted(AUDIENCES)}")
    base = _base(iso3)
    new_reports, _, window, _ = change_sets(iso3, days, today or date.today())
    figures = key_figures(iso3)
    reports = retrieve([p for r in new_reports for p in chunk(r)], default_query=DEFAULT_QUERY, limit=UPDATE_REPORTS, per_report=1)
    sources = ([{"n": i + 1, "title": p["title"], "source": p["source"], "date": p["date"], "url": p["url"], "kind": "report"}
                for i, p in enumerate(reports)]
               + [{"n": len(reports) + i + 1, "title": f"{f['label']}: {f['text']}", "source": f["source"], "date": f["date"],
                   "url": f["url"], "kind": "figure"} for i, f in enumerate(figures)])
    key = [{"label": f["label"], "text": f["text"], "date": f["date"], "cites": [len(reports) + i + 1]} for i, f in enumerate(figures)]
    base.update(audience=audience, window=window, weekReports=len(new_reports), keyFigures=key)
    if not reports and not figures:
        return {**base, "mode": "none", "headline": "", **{k: [] for k in UPDATE_SECTIONS}, "sources": [],
                "gaps": f"No reports from the last {days} days and no verified figures are saved for {base['country']}."}

    if reports and os.environ.get("BOB_API_KEY"):
        try:
            out = run_bob(build_update_prompt(base["country"], audience, window, reports, figures))
            return renumber_sources({**base, "mode": "ai", **validate_update(extract_answer(out), len(sources), len(reports)), "sources": sources})
        except Exception as e:
            print("Bob failed, using fallback:", str(e)[:200])
    return renumber_sources({**base, "mode": "fallback", **fallback_update(reports), "sources": sources})
