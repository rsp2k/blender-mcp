"""The free chat trial: a USD budget on the server's default backend (CHAT_TRIAL_USD).

Who is on the trial is decided in settings.on_trial; this module keeps the
spend. Before a turn, an account whose spend has reached the budget gets
``trial_ended``. During a trial turn every model call (rounds, retries,
vision descriptions) adds its cost (pricing.cost_usd) with an in-place
``used_usd = used_usd + :cost``, so concurrent calls sum exactly. A turn may
overshoot the budget by its own cost.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..storage.models import ChatTrialUsage
from .config import ChatConfig

logger = logging.getLogger(__name__)

ENDED_HINT = ("Your free trial is used up. Paste your Claude API key in the "
              "Chat panel to keep going.")


def view(cfg: ChatConfig, used_usd: float) -> dict:
    """The ``trial`` object the add-on sees. ``used`` is the real spend, which
    can pass the limit by the last turn's cost; ``remaining`` stops at 0."""
    limit = cfg.trial_usd
    used = max(0.0, float(used_usd or 0))
    remaining = max(0.0, limit - used)
    percent = math.floor(remaining / limit * 100 + 1e-9) if limit > 0 else 0
    return {"unit": "usd", "limit": limit, "used": round(used, 4),
            "remaining": round(remaining, 4), "percent_left": max(0, min(100, percent))}


def ended(cfg: ChatConfig, used_usd: float) -> bool:
    return used_usd >= cfg.trial_usd


async def used(session_factory: Callable[[], Any], user_sub: str) -> float:
    async with session_factory() as s:
        v = await s.scalar(select(ChatTrialUsage.used_usd)
                           .where(ChatTrialUsage.user_sub == user_sub))
    return float(v or 0)


async def status(session_factory: Callable[[], Any], cfg: ChatConfig, user_sub: str) -> dict:
    return view(cfg, await used(session_factory, user_sub))


async def _add(s, user_sub: str, cost: float, tokens_in: int, tokens_out: int) -> bool:
    res = await s.execute(
        update(ChatTrialUsage)
        .where(ChatTrialUsage.user_sub == user_sub)
        .values(used_usd=ChatTrialUsage.used_usd + cost,
                tokens_in=ChatTrialUsage.tokens_in + tokens_in,
                tokens_out=ChatTrialUsage.tokens_out + tokens_out,
                updated_at=datetime.now(UTC))
    )
    return res.rowcount == 1


async def spend(session_factory: Callable[[], Any], user_sub: str, cost: float,
                tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Add one model call's cost and tokens to the account's total."""
    cost = round(max(0.0, cost), 6)
    async with session_factory() as s:
        if not await _add(s, user_sub, cost, tokens_in, tokens_out):
            # First spend: create the row. A concurrent first spend may beat
            # us to it; then add through the UPDATE.
            s.add(ChatTrialUsage(user_sub=user_sub, used_usd=cost,
                                 tokens_in=tokens_in, tokens_out=tokens_out))
            try:
                await s.commit()
                return
            except IntegrityError:
                await s.rollback()
                await _add(s, user_sub, cost, tokens_in, tokens_out)
        await s.commit()


@dataclass(frozen=True)
class Meter:
    """Charges the model calls of one trial turn to its account. Set in
    routing.current_trial for the turn; the routing handler calls it."""

    session_factory: Callable[[], Any]
    user_sub: str

    async def charge(self, cost: float, tokens_in: int, tokens_out: int) -> None:
        if cost <= 0 and not tokens_in and not tokens_out:
            return
        try:
            await spend(self.session_factory, self.user_sub, cost, tokens_in, tokens_out)
        except Exception as e:  # noqa: BLE001 - counting must never break a turn
            logger.warning("chat trial spend not recorded: %s", type(e).__name__)
