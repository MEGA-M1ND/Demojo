"""OpenRouter client behaviour against a mocked transport (no network, no spend)."""

from __future__ import annotations

import json

import httpx
import pytest

from demojo import db
from demojo.ai_schemas import IMAGE_ANALYSIS_SCHEMA, ImageAnalysis
from demojo.config import get_settings, reset_settings_cache
from demojo.costs import Ledger, project_costs
from demojo.errors import AppError
from demojo.projects import create_project, update_project
from demojo.providers.openrouter import OpenRouterClient

VALID = {
    "kind": "software_screenshot", "summary": "A dashboard.", "visible_elements": ["chart"], "readable_text": ["Overdue"],
    "focus_areas": [{"label": "totals", "x": 0.1, "y": 0.2, "w": 0.5, "h": 0.2}], "suggested_role": "feature", "uncertainty": "",
}
ENDPOINTS = {"data": {"endpoints": [
    {"provider_name": "A", "pricing": {"prompt": "0.00000025", "completion": "0.0000015", "image": "0.00000025"}},
    {"provider_name": "B", "pricing": {"prompt": "0.00000045", "completion": "0.0000027"}},
]}}
TTS_ENDPOINTS = {"data": {"endpoints": [{"provider_name": "T", "pricing": {"prompt": "0.000004", "completion": "0"}}]}}


def chat_ok(content: str, cost: float | None = 0.0002) -> httpx.Response:
    usage = {"prompt_tokens": 900, "completion_tokens": 120}
    if cost is not None:
        usage["cost"] = cost
    return httpx.Response(200, json={"id": "gen-1", "choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": usage})


class Router:
    def __init__(self, chat=None, speech=None):
        self.chat = list(chat or [])
        self.speech = list(speech or [])
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/endpoints"):
            return httpx.Response(200, json=TTS_ENDPOINTS if "kokoro" in path else ENDPOINTS)
        if path.endswith("/chat/completions"):
            item = self.chat.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if path.endswith("/audio/speech"):
            item = self.speech.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if path.endswith("/generation"):
            return httpx.Response(200, json={"data": {"total_cost": 0.00001}})
        return httpx.Response(404, json={"error": {"code": 404, "message": "nope"}})

    @property
    def paid(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(("/chat/completions", "/audio/speech"))]


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-real")
    reset_settings_cache()
    return get_settings()


def client_for(router: Router, settings) -> OpenRouterClient:
    return OpenRouterClient(settings, transport=httpx.MockTransport(router), sleep=lambda s: None)


def analyze(c: OpenRouterClient, ledger: Ledger) -> ImageAnalysis:
    return c.chat_json(ledger, purpose="analyze_image", model="google/gemini-3.1-flash-lite",
                       messages=[{"role": "user", "content": "x"}], schema_name="image_analysis",
                       schema=IMAGE_ANALYSIS_SCHEMA, result_model=ImageAnalysis, max_tokens=800, images=1)


def calls(pid: str) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM provider_calls WHERE project_id = ? ORDER BY created_at", (pid,))]


def test_missing_key_fails_before_any_request(monkeypatch):
    reset_settings_cache()
    router = Router()
    c = client_for(router, get_settings())
    p = create_project()
    with pytest.raises(AppError) as e:
        analyze(c, Ledger(p["id"], None))
    assert e.value.code == "missing_key"
    assert router.paid == []


def test_structured_output_success_records_reported_cost(keyed):
    router = Router(chat=[chat_ok(json.dumps(VALID), cost=0.00031)])
    c = client_for(router, keyed)
    p = create_project()
    res = analyze(c, Ledger(p["id"], "job_x"))
    assert res.kind == "software_screenshot"
    body = json.loads(router.paid[0].content)
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"] is True
    assert body["provider"] == {"require_parameters": True}
    assert "Authorization" in router.paid[0].headers
    (call,) = calls(p["id"])
    assert call["status"] == "succeeded" and call["actual_usd"] == pytest.approx(0.00031) and call["cost_source"] == "provider_reported"
    assert call["estimated_usd"] and call["estimated_usd"] > 0


def test_invalid_output_gets_exactly_one_repair(keyed):
    router = Router(chat=[chat_ok("sorry, here you go: {oops"), chat_ok("```json\n" + json.dumps(VALID) + "\n```")])
    c = client_for(router, keyed)
    p = create_project()
    assert analyze(c, Ledger(p["id"], None)).summary == "A dashboard."
    assert len(router.paid) == 2
    assert [x["purpose"] for x in calls(p["id"])] == ["analyze_image", "repair"]

    router2 = Router(chat=[chat_ok("{}"), chat_ok('{"kind": "banana"}')])
    with pytest.raises(AppError) as e:
        analyze(client_for(router2, keyed), Ledger(p["id"], None))
    assert e.value.code == "provider_invalid_response" and len(router2.paid) == 2


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"error": {"code": 401, "message": "API key expired."}}, "invalid_key"),
        (402, {"error": {"code": 402, "message": "Insufficient credits"}}, "insufficient_credits"),
        (400, {"error": {"code": 400, "message": "google/x is not a valid model ID"}}, "incompatible_model"),
        (503, {"error": {"code": 503, "message": "No endpoints found that can handle the requested parameters"}}, "incompatible_model"),
        (403, {"error": {"code": 403, "message": "flagged by moderation"}}, "provider_refused"),
        (502, {"error": {"code": 502, "message": "upstream error"}}, "provider_unavailable"),
    ],
)
def test_http_errors_are_classified(keyed, status, body, code):
    router = Router(chat=[httpx.Response(status, json=body)])
    p = create_project()
    with pytest.raises(AppError) as e:
        analyze(client_for(router, keyed), Ledger(p["id"], None))
    assert e.value.code == code
    assert len(router.paid) == 1  # never auto-retried
    assert calls(p["id"])[0]["status"] == "failed"


def test_rate_limit_with_retry_after_is_retried(keyed):
    router = Router(chat=[httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow down"}}),
                          chat_ok(json.dumps(VALID))])
    p = create_project()
    analyze(client_for(router, keyed), Ledger(p["id"], None))
    assert len(router.paid) == 2


def test_read_timeout_is_ambiguous_and_not_retried(keyed):
    router = Router(chat=[httpx.ReadTimeout("slow"), chat_ok(json.dumps(VALID))])
    p = create_project()
    with pytest.raises(AppError) as e:
        analyze(client_for(router, keyed), Ledger(p["id"], "job_t"))
    assert e.value.code == "provider_timeout" and e.value.ambiguous
    assert len(router.paid) == 1
    assert calls(p["id"])[0]["status"] == "ambiguous"
    assert project_costs(p["id"])["ambiguous_calls"] == 1


def test_budget_is_checked_before_the_request_is_sent(keyed):
    router = Router(chat=[chat_ok(json.dumps(VALID))])
    p = create_project()
    update_project(p["id"], ai_budget_usd=0.0001)
    with pytest.raises(AppError) as e:
        analyze(client_for(router, keyed), Ledger(p["id"], None))
    assert e.value.code == "budget_exceeded"
    assert router.paid == []


def test_unpriced_model_stops_before_the_call(keyed):
    def no_price(request):
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(200, json={"data": {"endpoints": [{"pricing": {}}]}})
        raise AssertionError("paid request must not be sent")

    p = create_project()
    c = OpenRouterClient(keyed, transport=httpx.MockTransport(no_price), sleep=lambda s: None)
    with pytest.raises(AppError) as e:
        analyze(c, Ledger(p["id"], None))
    assert e.value.code == "pricing_unavailable"


def test_speech_validates_audio_and_records_generation_cost(keyed):
    mp3 = b"ID3" + b"\x00" * 4000
    router = Router(speech=[httpx.Response(200, content=mp3, headers={"content-type": "audio/mpeg", "x-generation-id": "gen-tts"})])
    p = create_project()
    audio, gen = client_for(router, keyed).speech(Ledger(p["id"], None), text="Hello there.", model="hexgrad/kokoro-82m", voice="af_heart")
    assert audio == mp3 and gen == "gen-tts"
    body = json.loads(router.paid[0].content)
    assert body == {"model": "hexgrad/kokoro-82m", "input": "Hello there.", "voice": "af_heart", "response_format": "mp3"}
    (call,) = calls(p["id"])
    assert call["actual_usd"] == pytest.approx(0.00001) and call["generation_id"] == "gen-tts"


def test_speech_json_instead_of_audio_is_an_error_not_silence(keyed):
    router = Router(speech=[httpx.Response(200, json={"oops": True})])
    p = create_project()
    with pytest.raises(AppError) as e:
        client_for(router, keyed).speech(Ledger(p["id"], None), text="Hi.", model="hexgrad/kokoro-82m", voice="af_heart")
    assert e.value.code == "narration_failed"
