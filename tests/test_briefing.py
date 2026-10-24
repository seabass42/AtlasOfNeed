# Run from the project folder: python -m pytest tests -q
import json

import pytest
from fastapi.testclient import TestClient

import briefing
from briefing import extract_answer, retrieve, validate
from main import app

client = TestClient(app)


def test_drops_uncited_and_fake_citations():
    out = validate({"bullets": [
        {"text": "Cited fact", "cites": [1, 2]},
        {"text": "Made-up fact with no source", "cites": []},
        {"text": "Fake citation", "cites": [99]},
    ], "gaps": "x"}, 3)
    assert len(out["bullets"]) == 1
    assert out["bullets"][0]["cites"] == [1, 2]


def test_rejects_output_with_no_cited_bullets():
    with pytest.raises(ValueError):
        validate({"bullets": [{"text": "no source", "cites": []}]}, 3)


def test_extracts_json_from_wrapped_bob_output():
    wrapped = json.dumps({"result": 'Here: {"bullets":[{"text":"a","cites":[1]}],"gaps":""}'})
    assert extract_answer(wrapped)["bullets"][0]["text"] == "a"


def test_retrieval_ranks_relevant_passage_first():
    p = [
        {"text": "Weather was mild and sports events continued as normal across the region this week.", "date": "2026-10-01"},
        {"text": "Food insecurity rose sharply and humanitarian access to displaced families remained limited.", "date": "2026-10-01"},
    ]
    assert "Food insecurity" in retrieve(p, "food access displaced")[0]["text"]


def test_api_conflicts():
    r = client.get("/api/conflicts")
    assert r.status_code == 200
    assert any(c["iso3"] == "SDN" and c["mapId"] == "729" for c in r.json()["countries"])


def test_api_briefing_fallback(monkeypatch):
    monkeypatch.delenv("BOB_API_KEY", raising=False)
    r = client.post("/api/briefing", json={"iso3": "SDN"})
    assert r.status_code == 200 and r.json()["mode"] == "fallback"


def test_api_briefing_ai_path(monkeypatch):
    monkeypatch.setenv("BOB_API_KEY", "test")
    monkeypatch.setattr(briefing, "run_bob", lambda prompt: json.dumps(
        {"result": '{"bullets":[{"text":"AI point","cites":[1]},{"text":"uncited","cites":[]}],"gaps":"g"}'}))
    d = client.post("/api/briefing", json={"iso3": "UKR"}).json()
    assert d["mode"] == "ai" and [b["text"] for b in d["bullets"]] == ["AI point"]


def test_api_rejects_bad_code():
    assert client.post("/api/briefing", json={"iso3": "../x"}).status_code == 400


def test_off_topic_question_retrieves_nothing():
    p = [{"text": "Food insecurity rose sharply among displaced families in Darfur.", "date": "2026-10-01", "url": "a"}]
    assert retrieve(p, "what is 1+1") == []
    assert retrieve(p, "what is the", "Sudan") == []  # filler words alone don't count as a match


def sudan_passages():
    return [p for r in briefing.load_reports("SDN") for p in briefing.chunk(r)]


def test_semantic_search_matches_meaning_not_words():
    if briefing.embedding_model() is None:
        pytest.skip("embedding model not available")
    p = [
        {"text": "Wheat and sorghum prices at the central market were stable this week.", "date": "2026-10-01", "url": "a"},
        {"text": "Thousands of families fled their villages and are now sheltering in overcrowded camps.", "date": "2026-10-01", "url": "b"},
    ]
    # No word in common with the camp passage, but the same meaning.
    for q in ("refugees", "people forced from homes"):
        assert briefing.retrieve(p, q)[0]["url"] == "b", q


def test_general_and_topical_questions_find_passages():
    if briefing.embedding_model() is None:
        pytest.skip("embedding model not available")
    passages = sudan_passages()
    for q in ("what is happening in Sudan", "how are refugees doing", "is there famine or starvation"):
        assert briefing.retrieve(passages, q, "Sudan"), q
    for q in ("what is 1+1", "best pizza recipe", "what is the capital of France"):
        assert briefing.retrieve(passages, q, "Sudan") == [], q


def test_keyword_fallback_when_model_unavailable(monkeypatch):
    monkeypatch.setattr(briefing, "embedding_model", lambda: None)
    p = [
        {"text": "Weather was mild and sports events continued as normal across the region this week.", "date": "2026-10-01", "url": "a"},
        {"text": "Food insecurity rose sharply and humanitarian access to displaced families remained limited.", "date": "2026-10-01", "url": "b"},
    ]
    assert briefing.retrieve(p, "food access")[0]["url"] == "b"
    assert briefing.retrieve(p, "what is 1+1") == []


def test_question_matches_word_variants():
    p = [{"text": "Thousands of refugees and displaced people crossed the border.", "date": "2026-10-01", "url": "a"}]
    assert retrieve(p, "how many refugee displacement") == p


def test_retrieval_caps_passages_per_report():
    p = [{"text": f"Food aid and humanitarian access update number {i}.", "date": "2026-10-01", "url": "same"} for i in range(5)]
    assert len(retrieve(p)) == briefing.MAX_PER_REPORT


def test_api_off_topic_question_skips_ai(monkeypatch):
    monkeypatch.setenv("BOB_API_KEY", "test")
    monkeypatch.setattr(briefing, "run_bob", lambda prompt: pytest.fail("Bob should not be called"))
    d = client.post("/api/briefing", json={"iso3": "SDN", "question": "what is 1+1"}).json()
    assert d["mode"] == "no_match" and d["bullets"] == [] and d["sources"] == [] and not d["answered"]


def test_ai_can_say_sources_dont_cover_question(monkeypatch):
    monkeypatch.setenv("BOB_API_KEY", "test")
    monkeypatch.setattr(briefing, "run_bob", lambda prompt: json.dumps(
        {"last_message": '{"bullets":[],"gaps":"The sources do not cover how many hospitals are working."}'}))
    d = client.post("/api/briefing", json={"iso3": "SDN", "question": "how many hospitals are working in Darfur"}).json()
    assert d["mode"] == "ai" and d["bullets"] == [] and not d["answered"] and "do not cover" in d["gaps"]


def test_api_profile():
    d = client.get("/api/profile/sdn").json()
    assert d["iso3"] == "SDN"
    facts = [f for s in d["sections"] for f in s["facts"]]
    assert facts and all(f["quote"] and f["url"].startswith("https://") and f["date"] for f in facts)
    assert client.get("/api/profile/xyz").status_code == 404
    assert client.get("/api/profile/x1").status_code == 400
