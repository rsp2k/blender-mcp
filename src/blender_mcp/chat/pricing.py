"""What a chat model call cost, in USD, for the free trial (CHAT_TRIAL_USD)
and the ``cost_usd`` attribute of ``llm.call`` events.

Prices are USD per million tokens, from
https://platform.claude.com/docs/en/about-claude/pricing as of 2026-10-01.
Update them here when Anthropic's prices change.

Any model not in the table, including the GPU gateway's and OpenAI-compatible
models, is priced at Claude Haiku 4.5 rates. That is a stand-in, not a real
price: it keeps a trial on a non-Claude server default draining at a sane
pace. A call whose provider reported no usage costs nothing.

Our requests cache with ``{"type": "ephemeral"}`` and no ``ttl`` (the 5 minute
default), and the advisor's own caching asks for ``ttl: "5m"``, so cache
writes are priced at the 5 minute rate unless the usage breaks them down by
TTL.
"""

from __future__ import annotations

from dataclasses import dataclass

from .providers import Usage


@dataclass(frozen=True)
class Rates:
    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


_FABLE_5_1 = Rates(10, 12.50, 20, 0.25, 50)
_FABLE_5 = Rates(10, 12.50, 20, 1, 50)
_OPUS_5_5 = Rates(4, 5, 8, 0.20, 20)
_OPUS = Rates(5, 6.25, 10, 0.50, 25)
_SONNET_5 = Rates(2, 2.50, 4, 0.20, 10)
_SONNET_4 = Rates(3, 3.75, 6, 0.30, 15)
HAIKU_4_5 = Rates(1, 1.25, 2, 0.10, 5)

PRICES: dict[str, Rates] = {
    "claude-fable-5-1": _FABLE_5_1, "claude-mythos-5-1": _FABLE_5_1,
    "claude-fable-5": _FABLE_5, "claude-mythos-5": _FABLE_5,
    "claude-opus-5-5": _OPUS_5_5,
    "claude-opus-5": _OPUS, "claude-opus-4-8": _OPUS, "claude-opus-4-7": _OPUS,
    "claude-opus-4-6": _OPUS, "claude-opus-4-5": _OPUS,
    "claude-sonnet-5-5": _SONNET_5, "claude-sonnet-5": _SONNET_5,
    "claude-sonnet-4-6": _SONNET_4, "claude-sonnet-4-5": _SONNET_4,
    "claude-haiku-4-5": HAIKU_4_5,
}
# Unknown and non-Claude models.
FALLBACK = HAIKU_4_5


def rates_for(model: str | None) -> Rates:
    """Rates for a model id, a dated snapshot ("claude-haiku-4-5-20251001")
    or a Vertex id ("claude-haiku-4-5@20251001"); FALLBACK otherwise."""
    m = (model or "").strip().lower()
    for name, rates in PRICES.items():
        if m == name or m.startswith((name + "-20", name + "@")):
            return rates
    return FALLBACK


def _n(v: int | None) -> int:
    return v if isinstance(v, int) and v > 0 else 0


def cost_usd(usage: Usage | None, model: str | None, advisor_model: str | None = None, *,
             provider: str = "anthropic") -> float:
    """USD for one completion: the executor's tokens at its own rates, plus
    the advisor's input and output tokens at the advisor model's rates.

    Anthropic reports ``input_tokens`` without the cached part; OpenAI-style
    providers count cached tokens inside it, so for those the cached share is
    taken out of ``input_tokens`` before pricing.
    """
    if usage is None:
        return 0.0
    r = rates_for(model)
    inp, out = _n(usage.input_tokens), _n(usage.output_tokens)
    read = _n(usage.cache_read_input_tokens)
    if provider != "anthropic":
        read = min(read, inp)
        inp -= read
    w5, w1h = usage.cache_creation_5m_input_tokens, usage.cache_creation_1h_input_tokens
    if w5 is None and w1h is None:
        w5, w1h = _n(usage.cache_creation_input_tokens), 0
    else:
        w5, w1h = _n(w5), _n(w1h)
    micro = (inp * r.input + w5 * r.cache_write_5m + w1h * r.cache_write_1h
             + read * r.cache_read + out * r.output)
    a_in, a_out = _n(usage.advisor_input_tokens), _n(usage.advisor_output_tokens)
    if a_in or a_out:
        a = rates_for(advisor_model or model)
        micro += a_in * a.input + a_out * a.output
    return micro / 1_000_000


def tokens(usage: Usage | None, *, provider: str = "anthropic") -> tuple[int, int]:
    """(tokens in, tokens out) for the running totals: every input token the
    call was billed for, cached or not, plus the advisor's."""
    if usage is None:
        return 0, 0
    cached = _n(usage.cache_read_input_tokens) if provider == "anthropic" else 0
    tin = (_n(usage.input_tokens) + cached
           + _n(usage.cache_creation_input_tokens) + _n(usage.advisor_input_tokens))
    return tin, _n(usage.output_tokens) + _n(usage.advisor_output_tokens)
