from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import Any

import httpx
import openai
from pydantic import BaseModel, ValidationError

from app.services.settings import get_provider_secret

ThinkingMode = str

# A provider may keep emitting reasoning tokens for a long time. This is a
# memory bound only: it does not cancel the HTTP stream or impose a deadline.
# The user still gets the beginning of the thinking trace and the complete
# formal answer whenever the provider eventually returns one.
MAX_BUFFERED_STREAM_CHARS = 1_000_000


@dataclass(frozen=True)
class LlmTextResult:
    """Text plus the effective thinking policy used for an audited call."""

    content: str
    thinking_requested: bool
    thinking_used: bool
    thinking_fallback: bool = False
    fallback_reason: str | None = None
    thinking_content: str = ""
    formal_content: str = ""
    cancelled: bool = False


# The adaptive policy is intentionally conservative: only reasoning-heavy
# agent nodes opt in.  Deterministic validators and execution never call the
# LLM, and therefore never consume thinking tokens.
ADAPTIVE_THINKING_NODES = {
    "evidence_review",
    # Command composition and review need multi-step reasoning over topology,
    # handbook views and prerequisites. Intent extraction, JSON format repair,
    # lexical retrieval and deterministic compilation intentionally remain
    # non-thinking under the adaptive policy.
    "command_plan",
    "command_repair",
    "command_review",
    "result_diagnosis",
}

class ThinkingBudgetExceeded(RuntimeError):
    """The provider kept reasoning without producing a formal response."""


class FormalResponseTimeout(RuntimeError):
    """The provider did not start a usable response within the request budget."""


async def _request_non_stream_completion(client: openai.AsyncOpenAI, kwargs: dict[str, Any]) -> Any:
    """Await a compatible provider's ordinary completion without a task deadline."""

    return await client.chat.completions.create(**kwargs)


async def _next_stream_chunk(
    stream_iterator: Any,
    *,
    formal_received: bool,
    first_formal_deadline: float,
    response_deadline: float | None = None,
) -> Any:
    """Read a stream chunk; cancellation is handled by the caller between chunks."""

    del formal_received, first_formal_deadline, response_deadline
    return await anext(stream_iterator)


def should_enable_thinking(mode: ThinkingMode, node: str) -> bool:
    """Resolve the global setting into a per-node thinking decision."""

    normalized = (mode or "adaptive").strip().lower()
    if normalized == "always":
        return True
    if normalized in {"off", "disabled", "never"}:
        return False
    return node in ADAPTIVE_THINKING_NODES


def thinking_extra_body(enabled: bool) -> dict[str, Any]:
    """Build the two provider-facing thinking controls together.

    Qwen-compatible gateways commonly read ``chat_template_kwargs`` while
    DeepSeek-compatible gateways commonly read the ``thinking`` object.  Both
    are sent for every first attempt, including the explicit disabled form.
    A provider that rejects optional fields can still use the compatibility
    fallback below.
    """

    return {
        "chat_template_kwargs": {"enable_thinking": bool(enabled)},
        "thinking": {"type": "enabled" if enabled else "disabled"},
    }


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
    endpoint_suffixes = ("/v1/chat/completions", "/v1/embeddings")
    for suffix in endpoint_suffixes:
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)] + "/v1/"
    if stripped.endswith("/v1"):
        return stripped + "/"
    return stripped + "/"


class ThinkTagSplitter:
    """Split providers that stream reasoning inside <think> tags instead of a field."""

    def __init__(self) -> None:
        self._buffer = ""
        self._in_thinking = False

    @staticmethod
    def _suffix_prefix_length(value: str, marker: str) -> int:
        return max(
            (
                length
                for length in range(1, min(len(value), len(marker) - 1) + 1)
                if value.endswith(marker[:length])
            ),
            default=0,
        )

    def consume(self, value: str, *, final: bool = False) -> tuple[str, str]:
        self._buffer += value
        thinking_parts: list[str] = []
        formal_parts: list[str] = []
        while self._buffer:
            marker = "</think>" if self._in_thinking else "<think>"
            index = self._buffer.find(marker)
            if index >= 0:
                target = thinking_parts if self._in_thinking else formal_parts
                target.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(marker) :]
                self._in_thinking = not self._in_thinking
                continue
            if final:
                (thinking_parts if self._in_thinking else formal_parts).append(self._buffer)
                self._buffer = ""
                break
            keep = self._suffix_prefix_length(self._buffer, marker)
            emit = self._buffer[:-keep] if keep else self._buffer
            if emit:
                (thinking_parts if self._in_thinking else formal_parts).append(emit)
            self._buffer = self._buffer[-keep:] if keep else ""
            break
        return "".join(thinking_parts), "".join(formal_parts)


def create_async_client(base_url: str, api_key: str) -> openai.AsyncOpenAI:
    # `verify=False` is a confirmed project constraint for this environment.
    http_client = httpx.AsyncClient(
        verify=False,
        # Planning can be deliberately long for a locally hosted reasoning
        # model. The workflow has user cancellation, but no artificial wall
        # clock deadline that would discard a still-running answer.
        timeout=None,
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
    stream: bool = False,
    on_chunk: Callable[[str, str], None] | None = None,
    cancel_event: Event | None = None,
    max_tokens: int | None = None,
) -> LlmTextResult:
    """Call an OpenAI-compatible endpoint without an application time limit.

    The first attempt sends both common thinking controls.  If a compatible
    gateway rejects optional thinking fields, the same request is retried with
    each single-provider shape and finally without optional fields.  This does
    not change the model prompt, topology scope, or command evidence.
    """

    async def request_once(
        client: openai.AsyncOpenAI,
        *,
        extra_body: dict[str, Any],
        effective_thinking: bool,
        fallback_reason: str | None,
    ) -> LlmTextResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "extra_body": extra_body,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max(1, int(max_tokens))
        # The configured local provider returns thinking reliably through its
        # stream protocol.  Interactive callers use those chunks directly;
        # batch callers still consume the stream internally and receive the
        # assembled formal response.  This does not add an application timeout.
        effective_stream = stream or effective_thinking
        if not effective_stream:
            response = await _request_non_stream_completion(client, kwargs)
            message = response.choices[0].message if response.choices else None
            content = str(getattr(message, "content", None) or "")
            thinking_content = str(
                getattr(message, "reasoning_content", None)
                or getattr(message, "reasoning", None)
                or ""
            )
            return LlmTextResult(
                content=content,
                thinking_requested=thinking,
                thinking_used=effective_thinking,
                thinking_fallback=fallback_reason is not None,
                fallback_reason=fallback_reason,
                thinking_content=thinking_content,
                formal_content=content,
            )

        kwargs["stream"] = True
        response = await client.chat.completions.create(**kwargs)
        formal_parts: list[str] = []
        thinking_parts: list[str] = []
        formal_buffered = 0
        thinking_buffered = 0
        stream_truncated = False
        tag_splitter = ThinkTagSplitter()
        stream_iterator = response.__aiter__()
        while True:
            if cancel_event and cancel_event.is_set():
                close = getattr(response, "close", None)
                if close:
                    await close()
                return LlmTextResult(
                    content="".join(formal_parts),
                    thinking_requested=thinking,
                    thinking_used=effective_thinking,
                    thinking_fallback=fallback_reason is not None,
                    fallback_reason=fallback_reason,
                    thinking_content="".join(thinking_parts),
                    formal_content="".join(formal_parts),
                    cancelled=True,
                )
            try:
                chunk = await anext(stream_iterator)
            except StopAsyncIteration:
                break
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            formal = str(getattr(delta, "content", None) or "")
            reasoning = str(
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
                or ""
            )
            tag_thinking, tag_formal = tag_splitter.consume(formal)
            combined_thinking = f"{reasoning}{tag_thinking}"
            if tag_formal:
                remaining = MAX_BUFFERED_STREAM_CHARS - formal_buffered
                if remaining > 0:
                    formal_parts.append(tag_formal[:remaining])
                    formal_buffered += min(len(tag_formal), remaining)
                if len(tag_formal) > max(remaining, 0):
                    stream_truncated = True
            if combined_thinking:
                remaining = MAX_BUFFERED_STREAM_CHARS - thinking_buffered
                if remaining > 0:
                    thinking_parts.append(combined_thinking[:remaining])
                    thinking_buffered += min(len(combined_thinking), remaining)
                if len(combined_thinking) > max(remaining, 0):
                    stream_truncated = True
            if on_chunk and (tag_formal or combined_thinking):
                on_chunk(combined_thinking, tag_formal)
        tag_thinking, tag_formal = tag_splitter.consume("", final=True)
        if tag_formal:
            remaining = MAX_BUFFERED_STREAM_CHARS - formal_buffered
            if remaining > 0:
                formal_parts.append(tag_formal[:remaining])
                formal_buffered += min(len(tag_formal), remaining)
            if len(tag_formal) > max(remaining, 0):
                stream_truncated = True
        if tag_thinking:
            remaining = MAX_BUFFERED_STREAM_CHARS - thinking_buffered
            if remaining > 0:
                thinking_parts.append(tag_thinking[:remaining])
                thinking_buffered += min(len(tag_thinking), remaining)
            if len(tag_thinking) > max(remaining, 0):
                stream_truncated = True
        if on_chunk and (tag_formal or tag_thinking):
            on_chunk(tag_thinking, tag_formal)
        if stream_truncated and on_chunk:
            on_chunk("\n[thinking/正式输出过长，界面仅保留前 1000000 个字符；网络请求未被截断。]\n", "")
        content = "".join(formal_parts)
        return LlmTextResult(
            content=content,
            thinking_requested=thinking,
            thinking_used=effective_thinking,
            thinking_fallback=fallback_reason is not None,
            fallback_reason=fallback_reason,
            thinking_content="".join(thinking_parts),
            formal_content=content,
        )

    candidates: list[tuple[dict[str, Any], bool, str | None]] = [
        (thinking_extra_body(thinking), thinking, None),
        (
            {"chat_template_kwargs": {"enable_thinking": False}},
            False,
            "端点不接受完整 thinking 参数，已使用 chat_template_kwargs 兼容模式。",
        ),
        (
            {"thinking": {"type": "disabled"}},
            False,
            "端点不接受 chat_template_kwargs，已使用 thinking 兼容模式。",
        ),
        ({}, False, "端点不接受可选 thinking 参数，已使用标准 OpenAI 兼容模式。"),
    ]
    client = create_async_client(base_url, api_key)
    try:
        first_error: Exception | None = None
        for index, (extra_body, effective_thinking, fallback_reason) in enumerate(candidates):
            try:
                return await request_once(
                    client,
                    extra_body=extra_body,
                    effective_thinking=effective_thinking,
                    fallback_reason=fallback_reason if index else None,
                )
            except Exception as exc:
                if not _thinking_unsupported(exc):
                    raise
                if first_error is None:
                    first_error = exc
        raise RuntimeError(f"所有 thinking 参数兼容模式均被端点拒绝：{first_error}")
    finally:
        await client.close()


async def request_embeddings(
    *,
    base_url: str,
    api_key: str,
    model: str,
    inputs: list[str],
    dimensions: int | None = None,
) -> list[list[float]]:
    """Use the same fixed ``verify=False`` OpenAI-compatible transport as LLM calls."""

    client = create_async_client(base_url, api_key)
    try:
        kwargs: dict[str, Any] = {"model": model, "input": inputs}
        if dimensions is not None:
            kwargs["dimensions"] = max(1, int(dimensions))
        response = await client.embeddings.create(**kwargs)
        data = list(response.data)
        indexed = [getattr(item, "index", None) for item in data]
        if data and all(isinstance(index, int) for index in indexed):
            data.sort(key=lambda item: int(item.index))
        return [list(item.embedding) for item in data]
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
