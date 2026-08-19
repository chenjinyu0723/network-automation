from __future__ import annotations

import json

import keyring
from sqlalchemy.orm import Session

from app.models import Setting
from app.schemas import ProviderSettingsInput, ProviderSettingsResponse

KEYRING_SERVICE = "network-automation"
LLM_KEY_REF = "provider:llm_api_key"
EMBEDDING_KEY_REF = "provider:embedding_api_key"

VISIBLE_KEYS = {
    "llm_base_url",
    "llm_model",
    "llm_temperature",
    "llm_thinking_mode",
    "embedding_base_url",
    "embedding_model",
    "embedding_dimensions",
    "embedding_batch_size",
}


def _set_value(session: Session, key: str, value: str, *, is_secret_reference: bool = False) -> None:
    item = session.get(Setting, key)
    if item is None:
        session.add(Setting(key=key, value=value, is_secret_reference=is_secret_reference))
    else:
        item.value = value
        item.is_secret_reference = is_secret_reference


def _get_value(session: Session, key: str, default: str | None = None) -> str | None:
    item = session.get(Setting, key)
    return item.value if item else default


def _secret_configured(reference: str) -> bool:
    try:
        return bool(keyring.get_password(KEYRING_SERVICE, reference))
    except keyring.errors.KeyringError:
        # A settings read must stay available even if a platform keyring is temporarily unavailable.
        return False


def save_provider_settings(session: Session, payload: ProviderSettingsInput) -> ProviderSettingsResponse:
    values = payload.model_dump(exclude={"llm_api_key", "embedding_api_key"})
    for key, value in values.items():
        if value is not None:
            _set_value(session, key, json.dumps(value))
    if payload.llm_api_key:
        keyring.set_password(KEYRING_SERVICE, LLM_KEY_REF, payload.llm_api_key)
        _set_value(session, "llm_api_key_ref", LLM_KEY_REF, is_secret_reference=True)
    if payload.embedding_api_key:
        keyring.set_password(KEYRING_SERVICE, EMBEDDING_KEY_REF, payload.embedding_api_key)
        _set_value(session, "embedding_api_key_ref", EMBEDDING_KEY_REF, is_secret_reference=True)
    session.commit()
    return read_provider_settings(session)


def read_provider_settings(session: Session) -> ProviderSettingsResponse:
    def load(key: str, default: object = None) -> object:
        value = _get_value(session, key)
        loaded = json.loads(value) if value is not None else None
        return default if loaded is None else loaded

    return ProviderSettingsResponse(
        llm_base_url=load("llm_base_url"),  # type: ignore[arg-type]
        llm_model=load("llm_model"),  # type: ignore[arg-type]
        llm_temperature=load("llm_temperature", 0.2),  # type: ignore[arg-type]
        llm_thinking_mode=load("llm_thinking_mode", "adaptive"),  # type: ignore[arg-type]
        embedding_base_url=load("embedding_base_url"),  # type: ignore[arg-type]
        embedding_model=load("embedding_model"),  # type: ignore[arg-type]
        embedding_dimensions=load("embedding_dimensions"),  # type: ignore[arg-type]
        embedding_batch_size=load("embedding_batch_size", 2),  # type: ignore[arg-type]
        llm_api_key_configured=_secret_configured(
            _get_value(session, "llm_api_key_ref", LLM_KEY_REF) or LLM_KEY_REF
        ),
        embedding_api_key_configured=_secret_configured(
            _get_value(session, "embedding_api_key_ref", EMBEDDING_KEY_REF) or EMBEDDING_KEY_REF
        ),
    )


def get_provider_secret(kind: str) -> str | None:
    reference = LLM_KEY_REF if kind == "llm" else EMBEDDING_KEY_REF
    try:
        return keyring.get_password(KEYRING_SERVICE, reference)
    except keyring.errors.KeyringError:
        return None
