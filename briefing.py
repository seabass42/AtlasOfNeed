"""RAG briefing for Atlas of Need.

1. Load saved reports for a country (data/reports/<iso3>.json)
2. Split them into passages and pick the most relevant ones (keyword match + recency)
3. Ask IBM Bob (`bob run`) to summarize ONLY those passages, citing passage numbers
4. Validate: drop any sentence without a real citation
5. If Bob is missing, slow, or returns bad output, fall back to source excerpts
"""
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
REPORTS_DIR = DATA_DIR / "reports"
MAX_PASSAGES = 8
BOB_TIMEOUT_S = 60
# The briefing only needs the passages in the prompt, so Bob gets no tools (files, commands, MCP, subagents...).
BOB_TOOL_GROUPS_OFF = "read,edit,execute,mcp,skill,todo,subagent,mode"

STOP = set("the a an and or of to in on for with is are was were be by at from as that this it its into than then".split())
DEFAULT_QUERY = "displacement civilians humanitarian access food insecurity casualties health aid needs funding"


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


def retrieve(passages: list[dict], question: str = "") -> list[dict]:
    if not passages:
        return []
    q = set(tokens(question or DEFAULT_QUERY))
    newest = max(_parse_date(p.get("date", "")) for p in passages)

    def score(p):
        words = tokens(p["text"])
        hits = sum(1 for w in words if w in q)
        age = (newest - _parse_date(p.get("date", ""))).days if newest != date.min else 0
        return hits / math.sqrt(len(words) + 1) + max(0.0, 1 - age / 90) * 0.3

    return sorted(passages, key=score, reverse=True)[:MAX_PASSAGES]


def build_prompt(country: str, question: str, passages: list[dict]) -> str:
    numbered = "\n\n".join(f"[{i + 1}] ({p['source']}, {p['date']}) {p['text']}" for i, p in enumerate(passages))
    focus = f"Focus on this question: {question}\n" if question else ""
    return f"""You are writing a short humanitarian situation briefing about {country} for journalists, students and NGO staff.
Use ONLY the numbered passages below. Do not use any tools and do not use outside knowledge. Do not predict events, give military analysis,
take sides, or give safety or evacuation advice. If the passages do not cover something, say so.
{focus}
Return ONLY valid JSON, no other text, in this exact shape:
{{"bullets":[{{"text":"one factual sentence","cites":[1,2]}}],"gaps":"one sentence on what the sources do not cover"}}
Write 4 to 6 bullets. Every bullet must cite at least one passage number.

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
    if not bullets:
        raise ValueError("No cited bullets")
    gaps = ans.get("gaps") if isinstance(ans.get("gaps"), str) else ""
    return {"bullets": bullets[:6], "gaps": gaps[:300]}


def fallback(passages: list[dict]) -> dict:
    bullets = [{"text": re.split(r"(?<=[.!?])\s", p["text"])[0][:300], "cites": [i + 1]} for i, p in enumerate(passages[:4])]
    return {"bullets": bullets, "gaps": "AI summary unavailable; showing the opening sentence of the most relevant source passages."}


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
        return {**base, "mode": "none", "bullets": [], "gaps": "No briefing has been prepared for this country yet.", "sources": []}

    passages = retrieve([p for r in reports for p in chunk(r)], question)
    sources = [{"n": i + 1, "title": p["title"], "source": p["source"], "date": p["date"], "url": p["url"]} for i, p in enumerate(passages)]
    newest = max((s["date"] for s in sources), default="")

    if os.environ.get("BOB_API_KEY"):
        try:
            out = run_bob(build_prompt(base["country"], question, passages))
            return {**base, "mode": "ai", "newestSource": newest, **validate(extract_answer(out), len(passages)), "sources": sources}
        except Exception as e:  # any failure -> safe fallback
            print("Bob failed, using fallback:", str(e)[:200])
    return {**base, "mode": "fallback", "newestSource": newest, **fallback(passages), "sources": sources}
