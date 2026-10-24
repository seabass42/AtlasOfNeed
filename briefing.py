"""RAG briefing for Atlas of Need.

1. Load saved reports for a country (data/reports/<iso3>.json)
2. Split them into passages and pick the most relevant ones (embedding similarity + recency; keyword match if the
   embedding model can't be loaded)
3. Ask IBM Bob (`bob run`) to summarize ONLY those passages, citing passage numbers
4. Validate: drop any sentence without a real citation
5. If Bob is missing, slow, or returns bad output, fall back to source excerpts
"""
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
REPORTS_DIR = DATA_DIR / "reports"
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

# Keyword fallback, only used when the embedding model is unavailable.
STOP = set("the a an and or of to in on for with is are was were be by at from as that this it its into than then".split())
QUESTION_STOP = set("""what who whom whose when where why how which will would could should can does did doing done
    has have had about tell please there their they them any much many some more most also just like know give show
    explain describe happening going right now today currently current latest recent news update info information""".split())


def load_conflicts() -> dict:
    return json.loads((DATA_DIR / "conflicts.json").read_text(encoding="utf-8"))


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
        reports = load_reports(c["iso3"])
        if reports:
            passage_vectors([p for r in reports for p in chunk(r)])
    print("Embeddings ready")


def _freshness(passages: list[dict]) -> list[float]:
    """1.0 for the newest report, falling to 0 at 90 days older."""
    newest = max(_parse_date(p.get("date", "")) for p in passages)
    if newest == date.min:
        return [0.0] * len(passages)
    return [max(0.0, 1 - (newest - _parse_date(p.get("date", ""))).days / 90) for p in passages]


def _cap_per_report(ranked: list[dict]) -> list[dict]:
    picked, per_report = [], {}
    for p in ranked:
        key = p.get("url") or p.get("title")
        if per_report.get(key, 0) < MAX_PER_REPORT:
            per_report[key] = per_report.get(key, 0) + 1
            picked.append(p)
        if len(picked) == MAX_PASSAGES:
            break
    return picked


def retrieve(passages: list[dict], question: str = "", country: str = "") -> list[dict]:
    """Most relevant passages, at most MAX_PER_REPORT per report.

    Without a question, ranks against a default needs-focused query. With a question, returns [] when nothing in the
    reports is close enough to answer it, so an off-topic question gets no passages instead of random ones.
    """
    if not passages:
        return []
    model = embedding_model()
    if model is None:
        return keyword_retrieve(passages, question, country)
    query = _unit(list(model.query_embed([question or DEFAULT_QUERY])))[0]
    similarity = passage_vectors(passages) @ query
    if question and similarity.max() < MIN_SIMILARITY:
        return []
    scores = [sim + RECENCY_WEIGHT * fresh for sim, fresh in zip(similarity.tolist(), _freshness(passages))]
    order = sorted(range(len(passages)), key=lambda i: scores[i], reverse=True)
    return _cap_per_report([passages[i] for i in order if not question or similarity[i] >= MIN_PASSAGE_SIMILARITY])


def keyword_retrieve(passages: list[dict], question: str = "", country: str = "") -> list[dict]:
    """Fallback: rank by shared words. Filler words and the country's own name don't count as a match."""
    skip = QUESTION_STOP | set(tokens(country))
    q = {w[:6] for w in tokens(question or DEFAULT_QUERY) if not question or w not in skip}  # w[:6]: crude stem
    if not q:
        return []

    def hits(p):
        return sum(1 for w in tokens(p["text"]) if w[:6] in q)

    scored = [(hits(p) / math.sqrt(len(tokens(p["text"])) + 1) + 0.3 * fresh, p)
              for p, fresh in zip(passages, _freshness(passages)) if not question or hits(p) > 0]
    return _cap_per_report([p for _, p in sorted(scored, key=lambda x: x[0], reverse=True)])


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


def build_briefing(iso3: str, question: str = "") -> dict:
    meta = next((c for c in load_conflicts()["countries"] if c["iso3"] == iso3), None)
    base = {
        "iso3": iso3,
        "country": meta["name"] if meta else iso3,
        "links": meta.get("officialLinks", []) if meta else [],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
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
