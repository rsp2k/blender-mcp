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
from dataclasses import replace
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..storage.models import ChatSettings
from . import advisors
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


def server_default(cfg: ChatConfig) -> Backend | None:
    """The server-wide backend (CHAT_DEFAULT_*), None when it lacks what it needs."""
    if cfg.default_provider == "anthropic":
        if not cfg.anthropic_api_key:
            return None
        return Backend("anthropic", cfg.default_model or DEFAULT_ANTHROPIC_MODEL,
                       DEFAULT_ANTHROPIC_BASE_URL, cfg.anthropic_api_key)
    if cfg.default_provider == "openai":
        if not cfg.default_base_url or not cfg.default_model:
            return None
        return Backend("openai", cfg.default_model, cfg.default_base_url, cfg.default_api_key)
    if not cfg.gpu_api_key:
        return None
    return Backend("gateway", cfg.default_model or cfg.model, cfg.gpu_base_url, cfg.gpu_api_key)


def _own(row: ChatSettings | None) -> bool:
    """A saved backend of the user's own (their key or URL). A saved "gateway"
    row only means "the server's backend", so it follows the server default."""
    return row is not None and row.provider in ("anthropic", "openai")


def public_view(row: ChatSettings | None, cfg: ChatConfig) -> dict:
    """What the add-on may see. Never a key, the server's or the user's."""
    if _own(row):
        model = row.model or {"anthropic": DEFAULT_ANTHROPIC_MODEL}.get(row.provider)
        return {
            "provider": row.provider,
            "model": model,
            "base_url": row.base_url,
            "has_key": bool(row.api_key_enc),
            "source": "user",
            **advisor_view(model, row.provider, row, cfg),
        }
    provider = cfg.default_provider
    if provider == "gateway":
        model = (row.model if row is not None else None) or cfg.default_model or cfg.model
        has_key = False
    elif provider == "anthropic":
        model = cfg.default_model or DEFAULT_ANTHROPIC_MODEL
        has_key = bool(cfg.anthropic_api_key)
    else:
        model = cfg.default_model or None
        has_key = bool(cfg.default_api_key)
    return {"provider": provider, "model": model, "base_url": None,
            "has_key": has_key, "source": "server",
            **advisor_view(model, provider, row, cfg)}


def _advisor_choice(row: ChatSettings | None) -> str | None:
    return getattr(row, "advisor", None) if row is not None else None


def with_advisor(backend: Backend | None, row: ChatSettings | None, cfg: ChatConfig) -> Backend | None:
    """The backend with the advisor it may use this turn (Claude only)."""
    if backend is None or backend.provider != "anthropic":
        return backend
    pick = advisors.effective(backend.model, _advisor_choice(row), cfg.anthropic_advisor,
                              cfg.anthropic_advisors)
    return replace(backend, advisor=pick)


def advisor_view(model: str | None, provider: str, row: ChatSettings | None,
                 cfg: ChatConfig) -> dict:
    """What the add-on shows for escalation: the options for this model,
    the account's saved choice, and what a turn would actually use."""
    if provider != "anthropic" or not model:
        return {"advisor": "", "advisor_choice": _advisor_choice(row), "advisors": []}
    return {
        "advisor": advisors.effective(model, _advisor_choice(row), cfg.anthropic_advisor,
                                      cfg.anthropic_advisors),
        "advisor_choice": _advisor_choice(row),
        "advisor_default": advisors.effective(model, None, cfg.anthropic_advisor,
                                              cfg.anthropic_advisors),
        "advisors": advisors.allowed_for(model, cfg.anthropic_advisors),
    }


def backend_for(row: ChatSettings | None, cfg: ChatConfig, user_sub: str | None) -> Backend | None:
    return with_advisor(_backend_for(row, cfg, user_sub), row, cfg)


def _backend_for(row: ChatSettings | None, cfg: ChatConfig, user_sub: str | None) -> Backend | None:
    """The backend this user's turns go to, or None when they have none.

    Precedence: the user's own saved backend, then the server default
    (CHAT_DEFAULT_PROVIDER, gated by CHAT_GATEWAY_USERS like the gateway
    always was). Raises ProviderError when a stored key can't be decrypted.
    """
    if row is not None and row.provider == "anthropic":
        return Backend("anthropic", row.model or DEFAULT_ANTHROPIC_MODEL,
                       row.base_url or DEFAULT_ANTHROPIC_BASE_URL,
                       decrypt(cfg, row.api_key_enc) if row.api_key_enc else "")
    if row is not None and row.provider == "openai":
        return Backend("openai", row.model or "", row.base_url or "",
                       decrypt(cfg, row.api_key_enc) if row.api_key_enc else "")
    if not cfg.user_allowed(user_sub):
        return None
    backend = server_default(cfg)
    if backend is not None and backend.provider == "gateway" and row is not None and row.model:
        backend = Backend("gateway", row.model, backend.base_url, backend.api_key)
    return backend


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
    advisor: str | None = None,
) -> ChatSettings | None:
    """Create, update or (``clear``) delete the user's row. Returns the row.
    ``advisor``: None leaves it as saved, "" follows the server, "off" or a
    model id sets it."""
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
        if advisor is not None:
            row.advisor = check_advisor(advisor, row, cfg)
        await s.commit()
        return row


def check_advisor(advisor: str, row: ChatSettings | None, cfg: ChatConfig) -> str | None:
    """Validate an advisor choice against the account's current model."""
    pick = advisors.base_model(advisor)
    if not pick:
        return None
    if pick == advisors.OFF:
        return advisors.OFF
    backend = _backend_for(row, cfg, "check") if row is not None else server_default(cfg)
    model = backend.model if backend is not None and backend.provider == "anthropic" else ""
    if not model:
        raise SettingsError("invalid_argument", "escalation needs a Claude model")
    allowed = advisors.allowed_for(model, cfg.anthropic_advisors)
    if pick not in allowed:
        raise SettingsError("invalid_argument",
                            f"{model} can't escalate to {pick} here; choose one of {allowed}")
    return pick


async def save_advisor(session_factory: Callable[[], Any], cfg: ChatConfig, user_sub: str,
                       advisor: str) -> ChatSettings | None:
    """Set only the advisor. An account without a saved backend gets a
    "gateway" row, which keeps following the server's backend."""
    async with session_factory() as s:
        row = await s.get(ChatSettings, user_sub)
        value = check_advisor(advisor, row, cfg)
        if row is None:
            if value is None:
                return None
            row = ChatSettings(user_sub=user_sub, provider="gateway")
            s.add(row)
        row.advisor = value
        await s.commit()
        return row
