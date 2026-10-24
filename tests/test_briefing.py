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
