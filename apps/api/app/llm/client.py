from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import openai
from pydantic import BaseModel, ValidationError

from app.services.settings import get_provider_secret

ThinkingMode = str


@dataclass(frozen=True)
class LlmTextResult:
    """Text plus the effective thinking policy used for an audited call."""

    content: str
    thinking_requested: bool
    thinking_used: bool
    thinking_fallback: bool = False
    fallback_reason: str | None = None


# The adaptive policy is intentionally conservative: only reasoning-heavy
# agent nodes opt in.  Deterministic validators and execution never call the
# LLM, and therefore never consume thinking tokens.
ADAPTIVE_THINKING_NODES = {
    "intent_refinement",
    "retrieval_planning",
    "evidence_review",
    "command_plan",
    "command_review",
    "result_diagnosis",
}


def should_enable_thinking(mode: ThinkingMode, node: str) -> bool:
    """Resolve the global setting into a per-node thinking decision."""

    normalized = (mode or "adaptive").strip().lower()
    if normalized == "always":
        return True
    if normalized in {"off", "disabled", "never"}:
        return False
    return node in ADAPTIVE_THINKING_NODES


def thinking_extra_body(enabled: bool) -> dict[str, Any]:
    """Build the project-standard OpenAI-compatible thinking payload."""

    body: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": bool(enabled)}}
    if enabled:
        body["thinking"] = {"type": "enabled"}
    return body


def _thinking_unsupported(exc: Exception) -> bool:
    """Detect an endpoint rejecting optional thinking fields.

    We only retry 400/404/422-style parameter errors mentioning the optional
    fields. Authentication, timeout and model errors must surface unchanged.
    """

    status = getattr(exc, "status_code", None)
    if status not in {400, 404, 422}:
        return False
    message = str(exc).lower()
    unsupported_terms = ("thinking", "chat_template_kwargs", "extra_body", "unknown field")
    return any(term in message for term in unsupported_terms)


def normalize_base_url(value: str) -> str:
    stripped = value.rstrip("/")
    suffix = "/v1/chat/completions"
    if stripped.endswith(suffix):
        return stripped[: -len("/chat/completions")] + "/"
    if stripped.endswith("/v1"):
        return stripped + "/"
    return stripped + "/"


def create_async_client(base_url: str, api_key: str) -> openai.AsyncOpenAI:
    # `verify=False` is a confirmed project constraint for this environment.
    http_client = httpx.AsyncClient(
        verify=False,
        timeout=300.0,
        transport=httpx.AsyncHTTPTransport(verify=False),
    )
    return openai.AsyncOpenAI(api_key=api_key, base_url=normalize_base_url(base_url), http_client=http_client)


async def request_text(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    thinking: bool = False,
) -> str:
    result = await request_text_result(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        temperature=temperature,
        thinking=thinking,
    )
    return result.content


async def request_text_result(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float,
    thinking: bool = False,
) -> LlmTextResult:
    """Call an OpenAI-compatible endpoint with optional thinking.

    Some compatible servers reject the optional ``thinking`` body.  A single
    safe retry without thinking preserves compatibility while recording the
    downgrade for the planning audit.
    """

    client = create_async_client(base_url, api_key)
    try:
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "extra_body": thinking_extra_body(thinking),
        }
        try:
            response = await client.chat.completions.create(**kwargs)
            return LlmTextResult(
                content=response.choices[0].message.content if response.choices else "",
                thinking_requested=thinking,
                thinking_used=thinking,
            )
        except Exception as exc:
            if not thinking or not _thinking_unsupported(exc):
                raise
            kwargs["extra_body"] = thinking_extra_body(False)
            response = await client.chat.completions.create(**kwargs)
            return LlmTextResult(
                content=response.choices[0].message.content if response.choices else "",
                thinking_requested=True,
                thinking_used=False,
                thinking_fallback=True,
                fallback_reason=f"端点不接受 thinking 参数，已降级：{str(exc)[:180]}",
            )
    finally:
        await client.close()


async def request_embeddings(
    *,
    base_url: str,
    api_key: str,
    model: str,
    inputs: list[str],
) -> list[list[float]]:
    """Use the same fixed ``verify=False`` OpenAI-compatible transport as LLM calls."""

    client = create_async_client(base_url, api_key)
    try:
        response = await client.embeddings.create(model=model, input=inputs)
        return [list(item.embedding) for item in response.data]
    finally:
        await client.close()


def parse_json_response(answer: str, schema: type[BaseModel]) -> BaseModel:
    """Parse a model JSON object without treating model text as executable tool calls."""

    candidate = answer.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[1] if "\n" in candidate else ""
        candidate = candidate.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("模型未返回合法 JSON；不会执行任何工具。") from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise ValueError("模型 JSON 不符合节点 Schema；不会执行任何工具。") from exc


def configured_llm_secret() -> str | None:
    return get_provider_secret("llm")
