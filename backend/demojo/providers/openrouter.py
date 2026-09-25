"""OpenRouter integration (the only paid AI provider).

Endpoints used (documentation checked 2026-09-25):
* POST /api/v1/chat/completions — image inputs as base64 data URLs; structured
  output via response_format json_schema; provider.require_parameters so
  routing only uses endpoints that support the requested parameters.
* POST /api/v1/audio/speech — returns raw audio bytes (mp3 requested).
* GET  /api/v1/models, /api/v1/models/{id}/endpoints — capabilities, voices, pricing.
* GET  /api/v1/key — key validity/limits (diagnostics only).
* GET  /api/v1/generation?id= — cost of a finished generation (used for TTS).

Retry policy: only requests that provably never reached a provider are
retried automatically (connection failures, 429/503 with Retry-After). A read
timeout after sending is *ambiguous* (it may have been charged) and is never
retried automatically.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..costs import (
    Ledger,
    Pricing,
    cached_pricing,
    estimate_chat_usd,
    estimate_tts_usd,
    pricing_from_endpoints,
    store_pricing,
)
from ..errors import AppError

T = TypeVar("T", bound=BaseModel)
SemanticCheck = Callable[[BaseModel], list[str]]


def _err_message(resp: httpx.Response) -> tuple[str, dict]:
    try:
        body = resp.json()
        err = body.get("error") or {}
        return str(err.get("message") or resp.text[:300]), err.get("metadata") or {}
    except (ValueError, AttributeError):
        return resp.text[:300], {}


def classify_http_error(resp: httpx.Response, model: str | None = None) -> AppError:
    msg, meta = _err_message(resp)
    code = resp.status_code
    low = msg.lower()
    retry_after = resp.headers.get("retry-after")
    detail = {"status": code, "provider_message": msg[:500], "model": model}
    if code == 401:
        hint = " The key has expired." if "expired" in low else ""
        return AppError("invalid_key", f"OpenRouter rejected the API key.{hint} Update OPENROUTER_API_KEY in the server .env and restart.", detail=detail)
    if code == 402:
        if meta.get("limit_source") == "openrouter_in_flight_budget":
            return AppError("rate_limited", "OpenRouter's in-flight spending budget is momentarily full. Retry shortly.",
                            detail=detail | {"retry_after": retry_after}, retryable=True)
        return AppError("insufficient_credits", "OpenRouter reports insufficient credits (or a per-key credit limit). Add credits or raise the key limit, then retry.", detail=detail)
    if code == 403:
        return AppError("provider_refused", f"OpenRouter refused the request: {msg[:200]}", detail=detail)
    if code == 408:
        return AppError("provider_timeout", "The AI request timed out on OpenRouter's side. It may or may not have been charged.",
                        detail=detail, retryable=True, ambiguous=True)
    if code == 429:
        return AppError("rate_limited", "OpenRouter rate limit reached. Wait a moment and retry.", detail=detail | {"retry_after": retry_after}, retryable=True)
    if code in (400, 404, 413, 422):
        return AppError("incompatible_model", f"The request was rejected for model {model}: {msg[:240]}", detail=detail)
    if code == 503 and ("requirement" in low or "no endpoints" in low or "no allowed providers" in low):
        return AppError("incompatible_model", f"No OpenRouter provider for {model} supports the required parameters: {msg[:200]}", detail=detail)
    if code >= 500:
        return AppError("provider_unavailable", f"The AI provider is unavailable ({code}). Retry later.", detail=detail, retryable=True)
    return AppError("provider_invalid_response", f"Unexpected OpenRouter response {code}: {msg[:200]}", detail=detail)


def data_url(path: Path, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(content: str) -> object:
    text = _FENCE.sub("", content.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


class OpenRouterClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep):
        self.s = settings
        self.transport = transport
        self.sleep = sleep

    # -- plumbing ---------------------------------------------------------

    def _require_key(self) -> str:
        if not self.s.openrouter_api_key:
            raise AppError("missing_key", "OPENROUTER_API_KEY is not set on the server. Add it to .env and restart the API and worker.")
        return self.s.openrouter_api_key

    def _client(self, read_timeout: float) -> httpx.Client:
        return httpx.Client(
            base_url=self.s.openrouter_base_url,
            timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=60.0, pool=10.0),
            transport=self.transport,
            headers={
                "Authorization": f"Bearer {self._require_key()}",
                "HTTP-Referer": self.s.app_referer,
                "X-Title": self.s.app_title,
            },
        )

    def _request(self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None,
                 read_timeout: float = 60.0, model: str | None = None, paid: bool = False) -> httpx.Response:
        attempts = 0
        while True:
            attempts += 1
            try:
                with self._client(read_timeout) as c:
                    resp = c.request(method, path, json=json_body, params=params)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if attempts < 3:
                    self.sleep(1.5 * attempts)
                    continue
                raise AppError("provider_unavailable", f"Could not reach OpenRouter ({type(e).__name__}). Check network access.",
                               retryable=True) from e
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError) as e:
                raise AppError(
                    "provider_timeout",
                    "The AI request did not finish in time. It may still have been processed and charged, so Demojo did not retry automatically.",
                    retryable=True, ambiguous=paid,
                ) from e
            if resp.status_code in (429, 503) and attempts < 3:
                ra = resp.headers.get("retry-after")
                try:
                    wait = float(ra) if ra is not None else None
                except ValueError:
                    wait = None
                if wait is not None and wait <= 15:
                    self.sleep(max(0.5, wait))
                    continue
            if resp.status_code >= 400:
                raise classify_http_error(resp, model)
            return resp

    # -- catalog ----------------------------------------------------------

    def list_models(self, output_modalities: str | None = None) -> list[dict]:
        params = {"output_modalities": output_modalities} if output_modalities else None
        resp = self._request("GET", "/models", params=params)
        return resp.json().get("data") or []

    def model_endpoints(self, model: str) -> list[dict]:
        resp = self._request("GET", f"/models/{model}/endpoints", model=model)
        return (resp.json().get("data") or {}).get("endpoints") or []

    def key_info(self) -> dict:
        resp = self._request("GET", "/key")
        return resp.json().get("data") or {}

    def pricing(self, model: str, kind: str) -> Pricing:
        key = f"{kind}:{model}"
        p = cached_pricing(key)
        if p is not None:
            return p
        try:
            eps = self.model_endpoints(model)
        except AppError as e:
            if e.code in ("missing_key", "invalid_key"):
                raise
            return Pricing(model, "token", None, None, None, None, False, f"pricing lookup failed: {e.message}", time.time())
        p = pricing_from_endpoints(model, kind, eps)
        store_pricing(key, p)
        return p

    def generation_cost(self, generation_id: str) -> float | None:
        try:
            resp = self._request("GET", "/generation", params={"id": generation_id}, read_timeout=15)
        except AppError:
            return None
        data = resp.json().get("data") or {}
        cost = data.get("total_cost")
        return float(cost) if isinstance(cost, (int, float)) else None

    # -- chat with structured output -------------------------------------

    def _chat_once(self, ledger: Ledger, purpose: str, model: str, messages: list[dict], schema_name: str,
                   schema: dict, max_tokens: int, temperature: float, estimate: float | None, chars: int) -> str:
        self._require_key()
        call_id = ledger.reserve(purpose, model, estimate, chars=chars)
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
            "provider": {"require_parameters": True},
        }
        try:
            resp = self._request("POST", "/chat/completions", json_body=body, read_timeout=150, model=model, paid=True)
        except AppError as e:
            ledger.settle(call_id, "ambiguous" if e.ambiguous else "failed", error_code=e.code)
            raise
        try:
            data = resp.json()
        except ValueError as e:
            ledger.settle(call_id, "ambiguous", error_code="provider_invalid_response")
            raise AppError("provider_invalid_response", "OpenRouter returned a non-JSON response.") from e
        usage = data.get("usage") or {}
        cost = usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None
        if data.get("error"):
            err = data["error"]
            ledger.settle(call_id, "succeeded" if cost else "failed", actual_usd=cost, error_code="provider_error")
            raise AppError("provider_unavailable", f"The model reported an error: {str(err.get('message', err))[:200]}", retryable=True)
        ledger.settle(call_id, "succeeded", actual_usd=cost, generation_id=data.get("id"),
                      prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"))
        choices = data.get("choices") or []
        if not choices:
            raise AppError("provider_invalid_response", "The model returned no choices.")
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            refusal = msg.get("refusal")
            raise AppError("provider_invalid_response", f"The model returned an empty response{': ' + str(refusal)[:160] if refusal else ''}.")
        if choices[0].get("finish_reason") == "length":
            return content + "\n<<TRUNCATED>>"
        return content

    def chat_json(self, ledger: Ledger, *, purpose: str, model: str, messages: list[dict], schema_name: str, schema: dict,
                  result_model: type[T], max_tokens: int, temperature: float = 0.3, images: int = 0,
                  semantic_check: SemanticCheck | None = None) -> T:
        pricing = self.pricing(model, "chat")
        chars = sum(len(p.get("text", "")) if isinstance(p, dict) else len(str(p))
                    for m in messages for p in (m["content"] if isinstance(m["content"], list) else [m["content"]]))
        estimate = estimate_chat_usd(pricing, prompt_text_chars=chars, images=images, max_completion_tokens=max_tokens,
                                     repair_allowance=False)
        content = self._chat_once(ledger, purpose, model, messages, schema_name, schema, max_tokens, temperature, estimate, chars)
        problems = self._problems(content, result_model, semantic_check)
        if isinstance(problems, BaseModel):
            return problems  # type: ignore[return-value]
        # One bounded repair request (text only).
        repair_msgs = [
            {"role": "system", "content": "You repair JSON so it satisfies a JSON Schema and listed validation errors. "
                                          "Keep the original meaning; do not add new facts. Return only the JSON object."},
            {"role": "user", "content": (
                "Validation errors:\n- " + "\n- ".join(problems[:20]) +
                "\n\nPrevious output:\n" + content.replace("<<TRUNCATED>>", "")[:12000] +
                "\n\nReturn corrected JSON only. Keep narration concise if output was truncated.")},
        ]
        rchars = sum(len(m["content"]) for m in repair_msgs)
        restimate = estimate_chat_usd(pricing, prompt_text_chars=rchars, images=0, max_completion_tokens=max_tokens,
                                      repair_allowance=False)
        content2 = self._chat_once(ledger, "repair", model, repair_msgs, schema_name, schema, max_tokens, 0.1, restimate, rchars)
        result = self._problems(content2, result_model, semantic_check)
        if isinstance(result, BaseModel):
            return result  # type: ignore[return-value]
        raise AppError(
            "provider_invalid_response",
            "The AI response did not match the required structure even after one repair attempt. Your inputs are unchanged; you can retry.",
            detail={"errors": result[:10]},
            retryable=True,
        )

    @staticmethod
    def _problems(content: str, result_model: type[T], semantic_check: SemanticCheck | None) -> T | list[str]:
        if content.endswith("<<TRUNCATED>>"):
            return ["the response was cut off because it exceeded the token limit; be more concise"]
        try:
            raw = extract_json(content)
        except (ValueError, json.JSONDecodeError) as e:
            return [f"response is not valid JSON: {e}"]
        try:
            obj = result_model.model_validate(raw)
        except ValidationError as e:
            return [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:20]]
        if semantic_check is not None:
            issues = semantic_check(obj)
            if issues:
                return issues
        return obj

    # -- speech -----------------------------------------------------------

    def speech(self, ledger: Ledger, *, text: str, model: str, voice: str, fmt: str = "mp3") -> tuple[bytes, str | None]:
        self._require_key()
        pricing = self.pricing(model, "speech")
        estimate = estimate_tts_usd(pricing, len(text))
        call_id = ledger.reserve("tts", model, estimate, chars=len(text))
        body = {"model": model, "input": text, "voice": voice, "response_format": fmt}
        try:
            resp = self._request("POST", "/audio/speech", json_body=body, read_timeout=120, model=model, paid=True)
        except AppError as e:
            ledger.settle(call_id, "ambiguous" if e.ambiguous else "failed", error_code=e.code)
            raise
        ctype = resp.headers.get("content-type", "")
        gen_id = resp.headers.get("x-generation-id")
        if not ctype.startswith("audio/") and "octet-stream" not in ctype:
            ledger.settle(call_id, "ambiguous", generation_id=gen_id, error_code="provider_invalid_response")
            raise AppError("narration_failed", f"Speech endpoint returned '{ctype or 'unknown'}' instead of audio.")
        if len(resp.content) < 512:
            ledger.settle(call_id, "ambiguous", generation_id=gen_id, error_code="provider_invalid_response")
            raise AppError("narration_failed", "Speech endpoint returned an empty or truncated audio file.")
        actual = self.generation_cost(gen_id) if gen_id else None
        ledger.settle(call_id, "succeeded", actual_usd=actual, generation_id=gen_id)
        return resp.content, gen_id
