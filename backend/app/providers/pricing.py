"""Model pricing catalogue used for cost accounting.

Prices are per one million tokens in USD. Unknown models cost zero and are
flagged in usage metadata so operators can add them to the catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.providers.base import Usage


@dataclass(frozen=True, slots=True)
class Price:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


def _p(prompt: str, completion: str) -> Price:
    return Price(Decimal(prompt), Decimal(completion))


# Prefix -> price. The longest matching prefix wins so that dated model
# snapshots inherit the price of their family.
_CATALOGUE: dict[str, Price] = {
    # OpenAI
    "gpt-4o": _p("2.50", "10.00"),
    "gpt-4o-mini": _p("0.15", "0.60"),
    "gpt-4.1": _p("2.00", "8.00"),
    "gpt-4.1-mini": _p("0.40", "1.60"),
    "gpt-4.1-nano": _p("0.10", "0.40"),
    "gpt-4-turbo": _p("10.00", "30.00"),
    "gpt-4": _p("30.00", "60.00"),
    "gpt-3.5-turbo": _p("0.50", "1.50"),
    "o1": _p("15.00", "60.00"),
    "o1-mini": _p("1.10", "4.40"),
    "o3": _p("2.00", "8.00"),
    "o3-mini": _p("1.10", "4.40"),
    "o4-mini": _p("1.10", "4.40"),
    "text-embedding-3-small": _p("0.02", "0"),
    "text-embedding-3-large": _p("0.13", "0"),
    # Anthropic
    "claude-opus-4": _p("15.00", "75.00"),
    "claude-sonnet-4": _p("3.00", "15.00"),
    "claude-3-7-sonnet": _p("3.00", "15.00"),
    "claude-3-5-sonnet": _p("3.00", "15.00"),
    "claude-3-5-haiku": _p("0.80", "4.00"),
    "claude-3-opus": _p("15.00", "75.00"),
    "claude-3-haiku": _p("0.25", "1.25"),
    # Google
    "gemini-2.5-pro": _p("1.25", "10.00"),
    "gemini-2.5-flash": _p("0.30", "2.50"),
    "gemini-2.0-flash": _p("0.10", "0.40"),
    "gemini-1.5-pro": _p("1.25", "5.00"),
    "gemini-1.5-flash": _p("0.075", "0.30"),
}

_MTOK = Decimal(1_000_000)


def lookup_price(model: str) -> Price | None:
    """Return the catalogue price for a model name, longest prefix first."""
    normalised = model.strip().lower()
    for prefix in sorted(_CATALOGUE, key=len, reverse=True):
        if normalised == prefix or normalised.startswith(f"{prefix}-"):
            return _CATALOGUE[prefix]
    return None


def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    price = lookup_price(model)
    if price is None:
        return Decimal("0")
    cost = (Decimal(prompt_tokens) * price.input_per_mtok) / _MTOK
    cost += (Decimal(completion_tokens) * price.output_per_mtok) / _MTOK
    return cost.quantize(Decimal("0.000001"))


def apply_cost(model: str, usage: Usage) -> Usage:
    """Return a copy of ``usage`` with ``cost_usd`` filled in."""
    return Usage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cost_usd=calculate_cost(model, usage.prompt_tokens, usage.completion_tokens),
    )
