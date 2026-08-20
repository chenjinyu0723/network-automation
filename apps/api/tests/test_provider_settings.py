from __future__ import annotations

import pytest
from app.models import Setting
from app.schemas import ProviderSettingsInput
from app.services.settings import read_provider_settings, save_provider_settings
from pydantic import ValidationError


def test_embedding_batch_size_defaults_to_one(session) -> None:  # type: ignore[no-untyped-def]
    settings = read_provider_settings(session)

    assert settings.embedding_batch_size == 1


def test_embedding_batch_size_uses_default_one_for_legacy_null(session) -> None:  # type: ignore[no-untyped-def]
    session.add(Setting(key="embedding_batch_size", value="null"))
    session.commit()

    assert read_provider_settings(session).embedding_batch_size == 1


def test_embedding_batch_size_is_persisted_and_bounded(session) -> None:  # type: ignore[no-untyped-def]
    saved = save_provider_settings(session, ProviderSettingsInput(embedding_batch_size=7))

    assert saved.embedding_batch_size == 7
    assert read_provider_settings(session).embedding_batch_size == 7
    with pytest.raises(ValidationError):
        ProviderSettingsInput(embedding_batch_size=21)
