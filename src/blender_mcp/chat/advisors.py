"""Which models a Claude chat model may escalate to (the advisor tool).

The table is the API's own: an advisor must be Claude Sonnet 4.6 or better
and at least as capable as the executor, and some pairs are refused with a
400. CHAT_ANTHROPIC_ADVISORS narrows it for the whole server (cost control);
each account picks one of what's left, or none.
"""

from __future__ import annotations

# Most capable first; also the order the add-on lists them in.
LADDER = (
    "claude-mythos-5-1", "claude-fable-5-1", "claude-mythos-5", "claude-fable-5",
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
)

_TOP = LADDER[:6]  # Mythos 5.1 .. Opus 5

COMPATIBLE: dict[str, tuple[str, ...]] = {
    "claude-haiku-4-5": LADDER,
    "claude-sonnet-4-6": LADDER,
    "claude-sonnet-5": LADDER[:8] + ("claude-sonnet-5-5", "claude-sonnet-5"),
    "claude-sonnet-5-5": _TOP + ("claude-sonnet-5-5",),
    "claude-opus-4-6": LADDER[:10] + ("claude-sonnet-5",),
    "claude-opus-4-7": LADDER[:8] + ("claude-sonnet-5-5",),
    "claude-opus-4-8": LADDER[:8] + ("claude-sonnet-5-5",),
    "claude-opus-5": _TOP,
    "claude-opus-5-5": _TOP,
    "claude-fable-5": _TOP,
    "claude-mythos-5": _TOP,
    "claude-fable-5-1": ("claude-mythos-5-1", "claude-fable-5-1"),
    "claude-mythos-5-1": ("claude-mythos-5-1", "claude-fable-5-1"),
}

OFF = "off"

LABELS = {
    "claude-mythos-5-1": "Claude Mythos 5.1", "claude-fable-5-1": "Claude Fable 5.1",
    "claude-mythos-5": "Claude Mythos 5", "claude-fable-5": "Claude Fable 5",
    "claude-opus-5-5": "Claude Opus 5.5", "claude-opus-5": "Claude Opus 5",
    "claude-opus-4-8": "Claude Opus 4.8", "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4-6": "Claude Opus 4.6", "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-sonnet-5": "Claude Sonnet 5", "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
}


def base_model(model: str) -> str:
    """"claude-haiku-4-5-20251001" -> "claude-haiku-4-5"."""
    model = (model or "").strip()
    for known in COMPATIBLE:
        if model == known or model.startswith(known + "-2"):
            return known
    return model


def parse_allowlist(value: str) -> frozenset[str]:
    return frozenset(base_model(v) for v in (value or "").replace(" ", ",").split(",") if v.strip())


def allowed_for(executor: str, allowlist: frozenset[str] = frozenset()) -> list[str]:
    """Advisors this executor may escalate to, strongest first. Excludes the
    executor itself: advising yourself costs double for nothing."""
    ex = base_model(executor)
    options = [m for m in COMPATIBLE.get(ex, ()) if m != ex]
    if allowlist:
        options = [m for m in options if m in allowlist]
    return options


def effective(executor: str, choice: str | None, server_default: str,
              allowlist: frozenset[str] = frozenset()) -> str:
    """The advisor a turn uses: the account's choice (None = the server's),
    "" when off or when that model isn't allowed for this executor."""
    pick = server_default if choice is None else choice
    pick = base_model(pick or "")
    if not pick or pick == OFF:
        return ""
    return pick if pick in allowed_for(executor, allowlist) else ""
