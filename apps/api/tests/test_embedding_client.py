from __future__ import annotations

import asyncio
from types import SimpleNamespace

import app.api.routes as routes
from app.llm import client
from app.schemas import ProviderSettingsResponse


class _FakeEmbeddingApi:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    async def create(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[0.3, 0.4]),
                SimpleNamespace(index=0, embedding=[0.1, 0.2]),
            ]
        )


class _FakeClient:
    def __init__(self, embeddings: _FakeEmbeddingApi) -> None:
        self.embeddings = embeddings

    async def close(self) -> None:
        return None


def test_embedding_endpoint_url_is_normalized() -> None:
    assert client.normalize_base_url("http://embedding.example/v1/embeddings") == (
        "http://embedding.example/v1/"
    )
    assert client.normalize_base_url("http://llm.example/v1/chat/completions") == (
        "http://llm.example/v1/"
    )
    assert client.normalize_base_url("http://provider.example/v1/") == (
        "http://provider.example/v1/"
    )


def test_embedding_request_sends_dimensions_and_batch_input(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    api = _FakeEmbeddingApi()
    monkeypatch.setattr(client, "create_async_client", lambda *_args: _FakeClient(api))

    vectors = asyncio.run(
        client.request_embeddings(
            base_url="http://embedding.example/v1/embeddings",
            api_key="sk-test",
            model="Qwen3-Embedding-4B",
            inputs=["Why is the sky blue?", "你是谁?"],
            dimensions=2560,
        )
    )

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert api.kwargs == {
        "model": "Qwen3-Embedding-4B",
        "input": ["Why is the sky blue?", "你是谁?"],
        "dimensions": 2560,
    }


def test_embedding_dimensions_can_be_omitted_for_other_openai_compatible_models(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    api = _FakeEmbeddingApi()
    monkeypatch.setattr(client, "create_async_client", lambda *_args: _FakeClient(api))

    asyncio.run(
        client.request_embeddings(
            base_url="http://embedding.example/v1/",
            api_key="sk-test",
            model="other-embedding",
            inputs=["text"],
        )
    )

    assert api.kwargs == {"model": "other-embedding", "input": ["text"]}


def test_embedding_connection_test_returns_actual_vector_dimensions(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = ProviderSettingsResponse(
        llm_base_url=None,
        llm_model=None,
        llm_temperature=0.2,
        llm_thinking_mode="adaptive",
        embedding_base_url="http://embedding.example/v1/embeddings",
        embedding_model="Qwen3-Embedding-4B",
        embedding_dimensions=2560,
        embedding_batch_size=2,
        llm_api_key_configured=False,
        embedding_api_key_configured=True,
    )
    calls: dict[str, object] = {}

    async def fake_request_embeddings(**kwargs: object) -> list[list[float]]:
        calls.update(kwargs)
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(routes, "read_provider_settings", lambda _session: settings)
    monkeypatch.setattr(routes, "get_provider_secret", lambda _provider: "sk-test")
    monkeypatch.setattr(routes, "request_embeddings", fake_request_embeddings)

    result = routes.test_embedding_provider(object())

    assert result.status == "ok"
    assert result.model == "Qwen3-Embedding-4B"
    assert result.dimensions == 3
    assert result.requested_dimensions == 2560
    assert calls["inputs"] == ["network automation embedding connectivity check"]
    assert calls["dimensions"] == 2560
