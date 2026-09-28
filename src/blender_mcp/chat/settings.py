"""Per-user chat backend settings, with API keys encrypted at rest.

Keys are Fernet tokens. CHAT_SECRET_KEY may be a Fernet key or any other
string (then SHA-256 of it is the key), so an operator can use ``openssl rand
-hex 32``. Rotating CHAT_SECRET_KEY makes stored keys unreadable; affected
users see ``backend_error`` until they save their key again.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..storage.models import ChatSettings
from .config import DEFAULT_ANTHROPIC_BASE_URL, DEFAULT_ANTHROPIC_MODEL, ChatConfig
from .providers import PROVIDERS, Backend, ProviderError

MAX_KEY_LEN = 4096
MAX_MODEL_LEN = 128
MAX_URL_LEN = 512


class SettingsError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def fernet(secret: str) -> Fernet | None:
    if not secret:
        return None
    try:
        return Fernet(secret.encode())
    except (ValueError, TypeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def encrypt(cfg: ChatConfig, plaintext: str) -> str:
    f = fernet(cfg.secret_key)
    if f is None:
        raise SettingsError("secret_key_not_configured",
                            "This server can't store API keys (CHAT_SECRET_KEY is not set).")
    return f.encrypt(plaintext.encode()).decode()


def decrypt(cfg: ChatConfig, token: str) -> str:
    f = fernet(cfg.secret_key)
    if f is None:
        raise ProviderError("the server can't read stored API keys (CHAT_SECRET_KEY is not set)")
    try:
        return f.decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ProviderError("the saved API key can't be decrypted any more; save it again") from e


def public_view(row: ChatSettings | None, cfg: ChatConfig) -> dict:
    """What the add-on may see. Never the key."""
    if row is None:
        return {"provider": "gateway", "model": cfg.model, "base_url": None, "has_key": False}
    return {
        "provider": row.provider,
        "model": row.model or {"gateway": cfg.model,
                               "anthropic": DEFAULT_ANTHROPIC_MODEL}.get(row.provider),
        "base_url": row.base_url,
        "has_key": bool(row.api_key_enc),
    }


def backend_for(row: ChatSettings | None, cfg: ChatConfig, user_sub: str | None) -> Backend | None:
    """The backend this user's turns go to, or None when they have none.

    Raises ProviderError when a stored key can't be decrypted.
    """
    if row is not None and row.provider == "anthropic":
        return Backend("anthropic", row.model or DEFAULT_ANTHROPIC_MODEL,
                       row.base_url or DEFAULT_ANTHROPIC_BASE_URL,
                       decrypt(cfg, row.api_key_enc) if row.api_key_enc else "")
    if row is not None and row.provider == "openai":
        return Backend("openai", row.model or "", row.base_url or "",
                       decrypt(cfg, row.api_key_enc) if row.api_key_enc else "")
    if not cfg.gateway_allowed(user_sub):
        return None
    model = (row.model if row is not None else None) or cfg.model
    return Backend("gateway", model, cfg.gpu_base_url, cfg.gpu_api_key)


async def load(session_factory: Callable[[], Any], user_sub: str) -> ChatSettings | None:
    async with session_factory() as s:
        return await s.get(ChatSettings, user_sub)


def _clean(value: str | None, limit: int, name: str) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if len(value) > limit:
        raise SettingsError("invalid_argument", f"{name} is longer than {limit} characters")
    return value or None


async def save(
    session_factory: Callable[[], Any],
    cfg: ChatConfig,
    user_sub: str,
    provider: str,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    clear: bool = False,
) -> ChatSettings | None:
    """Create, update or (``clear``) delete the user's row. Returns the row."""
    async with session_factory() as s:
        row = await s.get(ChatSettings, user_sub)
        if clear:
            if row is not None:
                await s.delete(row)
                await s.commit()
            return None
        if provider not in PROVIDERS:
            raise SettingsError("invalid_provider", f"provider must be one of {list(PROVIDERS)}")
        model = _clean(model, MAX_MODEL_LEN, "model")
        base_url = _clean(base_url, MAX_URL_LEN, "base_url")
        key = _clean(api_key, MAX_KEY_LEN, "api_key")

        if provider == "openai":
            if not base_url or not base_url.startswith(("http://", "https://")):
                raise SettingsError("invalid_argument",
                                    "base_url must be an http:// or https:// URL for the openai provider")
            if not model:
                raise SettingsError("invalid_argument", "model is required for the openai provider")
        else:
            # The gateway URL is the server's; Anthropic's is fixed.
            base_url = None
        if provider == "gateway":
            key = None

        enc = encrypt(cfg, key) if key else None
        if enc is None and key is None and row is not None and row.provider == provider \
                and provider != "gateway" and api_key is None:
            enc = row.api_key_enc  # keep the saved key when only model/url change
        if provider == "anthropic" and not enc:
            raise SettingsError("api_key_required", "an Anthropic API key is required")

        if row is None:
            row = ChatSettings(user_sub=user_sub, provider=provider)
            s.add(row)
        row.provider = provider
        row.model = model
        row.base_url = base_url
        row.api_key_enc = enc
        await s.commit()
        return row
