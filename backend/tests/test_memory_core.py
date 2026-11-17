"""Unit tests for memory scoring and sensitive-data protection."""

from __future__ import annotations

from collections import Counter

import pytest

from app.memory.embeddings import cosine_similarity
from app.memory.scoring import (
    DEFAULT_WEIGHTS,
    combine,
    lexical_score,
    recency_score,
    tokenize,
    usage_score,
)
from app.memory.sensitivity import inspect, redact


def test_tokenize_drops_stopwords_and_short_words() -> None:
    assert tokenize("The quick brown fox is in a box") == ["quick", "brown", "fox", "box"]


def test_tokenize_lowercases() -> None:
    assert tokenize("PGVECTOR Memory") == ["pgvector", "memory"]


def test_lexical_score_rewards_overlap() -> None:
    query = Counter(tokenize("pgvector similarity search"))
    high = lexical_score(query, tokenize("use pgvector for similarity search"))
    low = lexical_score(query, tokenize("unrelated content about css"))
    assert high > low
    assert 0.0 <= low <= high <= 1.0


def test_lexical_score_is_zero_without_overlap() -> None:
    assert lexical_score(Counter(tokenize("alpha beta")), tokenize("gamma delta")) == 0.0


def test_lexical_score_is_zero_for_empty_inputs() -> None:
    assert lexical_score(Counter(), tokenize("anything")) == 0.0
    assert lexical_score(Counter(tokenize("anything")), []) == 0.0


def test_lexical_score_penalises_very_long_memories() -> None:
    query = Counter(tokenize("pgvector"))
    short = lexical_score(query, ["pgvector", "search"])
    long = lexical_score(query, ["pgvector"] + ["filler"] * 200)
    assert short > long


def test_recency_decays_with_age() -> None:
    assert recency_score(0, 30) == 1.0
    half_life = recency_score(30 * 86_400, 30)
    assert half_life == pytest.approx(0.5, abs=1e-6)
    assert recency_score(120 * 86_400, 30) < half_life


def test_recency_handles_future_timestamps() -> None:
    assert recency_score(-500, 30) == 1.0


def test_usage_score_saturates() -> None:
    assert usage_score(0) == 0.0
    assert usage_score(5) > usage_score(1)
    assert usage_score(10_000) == pytest.approx(1.0)


def test_combine_without_vector_is_bounded() -> None:
    score = combine(lexical=1.0, recency=1.0, importance=1.0, usage=1.0, vector=None)
    assert 0.0 <= score <= 1.0


def test_combine_ranks_lexical_match_above_no_match() -> None:
    matched = combine(lexical=0.9, recency=0.5, importance=0.5, usage=0.2, vector=None)
    unmatched = combine(lexical=0.0, recency=0.5, importance=0.5, usage=0.2, vector=None)
    assert matched > unmatched


def test_combine_uses_vector_when_present() -> None:
    strong = combine(lexical=0.0, recency=0.0, importance=0.0, usage=0.0, vector=1.0)
    weak = combine(lexical=0.0, recency=0.0, importance=0.0, usage=0.0, vector=0.0)
    assert strong > weak


def test_weights_renormalise_to_one() -> None:
    weights = DEFAULT_WEIGHTS.normalised()
    total = weights.lexical + weights.recency + weights.importance + weights.usage
    assert total == pytest.approx(1.0)


def test_layer_boost_cannot_exceed_one() -> None:
    score = combine(
        lexical=1.0, recency=1.0, importance=1.0, usage=1.0, vector=None, layer_boost=0.9
    )
    assert score == 1.0


def test_cosine_similarity_of_identical_vectors_is_one() -> None:
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_rejects_mismatched_or_empty() -> None:
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


@pytest.mark.parametrize(
    "text",
    [
        'api_key = "abcdef123456"',
        "password: hunter2hunter2",
        "token=abc123456789",
        "Authorization: Bearer abcdefghijklmnop",
        "sk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "-----BEGIN RSA PRIVATE KEY-----",
        "postgres://user:supersecret@db.internal:5432/app",
        "aos_abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_inspect_flags_credentials(text: str) -> None:
    assert inspect(text).sensitive, text


@pytest.mark.parametrize(
    "text",
    [
        "The service uses pgvector for similarity search.",
        "Deploy with docker compose up -d api.",
        "Tokens expire after sixty minutes; refresh with the refresh endpoint.",
        "Set the API key in the provider settings screen, not in memory.",
        "",
    ],
)
def test_inspect_allows_ordinary_text(text: str) -> None:
    assert not inspect(text).sensitive, text


def test_verdict_is_truthy_when_sensitive() -> None:
    assert bool(inspect("token=abc123456789")) is True
    assert bool(inspect("plain note")) is False


def test_redact_removes_secrets_but_keeps_shape() -> None:
    text = "Use sk-abcdefghijklmnopqrstuvwx for now"
    cleaned = redact(text)
    assert "sk-abcdefghijklmnopqrstuvwx" not in cleaned
    assert "[REDACTED TOKEN]" in cleaned


def test_redact_keeps_key_names() -> None:
    cleaned = redact('api_key = "abcdef123456"')
    assert cleaned.startswith("api_key=")
    assert "abcdef123456" not in cleaned


def test_redact_leaves_safe_text_untouched() -> None:
    text = "Remember to run the migrations before deploying."
    assert redact(text) == text
