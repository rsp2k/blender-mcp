"""Free chat turns on the server's default backend (CHAT_TRIAL_TURNS).

Who is on the trial is decided in settings.on_trial; this module keeps the
count. A turn is spent with a guarded UPDATE (``turns_used < limit``), so
two turns racing for the last free message can't both get it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..storage.models import ChatTrialUsage
from .config import ChatConfig

ENDED_HINT = ("Your free messages are used up. Paste your Claude API key in the "
              "Chat panel to keep going.")


def view(cfg: ChatConfig, turns_used: int) -> dict:
    """The ``trial`` object the add-on sees. ``used`` never shows past the
    limit (the limit may have been lowered since)."""
    limit = cfg.trial_turns
    n = max(0, min(turns_used, limit))
    return {"limit": limit, "used": n, "remaining": limit - n}


async def used(session_factory: Callable[[], Any], user_sub: str) -> int:
    async with session_factory() as s:
        n = await s.scalar(select(ChatTrialUsage.turns_used)
                           .where(ChatTrialUsage.user_sub == user_sub))
    return int(n or 0)


async def status(session_factory: Callable[[], Any], cfg: ChatConfig, user_sub: str) -> dict:
    return view(cfg, await used(session_factory, user_sub))


async def _bump(s, user_sub: str, limit: int) -> bool:
    res = await s.execute(
        update(ChatTrialUsage)
        .where(ChatTrialUsage.user_sub == user_sub, ChatTrialUsage.turns_used < limit)
        .values(turns_used=ChatTrialUsage.turns_used + 1, updated_at=datetime.now(UTC))
    )
    return res.rowcount == 1


async def consume(session_factory: Callable[[], Any], cfg: ChatConfig,
                  user_sub: str) -> tuple[bool, dict]:
    """Spend one free turn. Returns (granted, trial view after the attempt)."""
    limit = cfg.trial_turns
    async with session_factory() as s:
        granted = await _bump(s, user_sub, limit)
        if not granted:
            exists = await s.scalar(select(ChatTrialUsage.user_sub)
                                    .where(ChatTrialUsage.user_sub == user_sub))
            if exists is None and limit > 0:
                # First turn: create the row. A concurrent first turn may
                # beat us to it; then spend through the guarded UPDATE.
                s.add(ChatTrialUsage(user_sub=user_sub, turns_used=1))
                try:
                    await s.commit()
                    return True, view(cfg, 1)
                except IntegrityError:
                    await s.rollback()
                    granted = await _bump(s, user_sub, limit)
        await s.commit()
    return granted, await status(session_factory, cfg, user_sub)
