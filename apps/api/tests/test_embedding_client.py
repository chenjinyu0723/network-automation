from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.llm import client


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
