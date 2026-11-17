"""Relevance scoring for memory retrieval.

Every factor is explicit and separate so a ranking can be explained: nothing is
an opaque model decision. The final score is a weighted sum in ``[0, 1]``.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9_]{2,}")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)


def tokenize(text: str) -> list[str]:
    return [word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS]


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    lexical: float = 0.45
    recency: float = 0.2
    importance: float = 0.2
    usage: float = 0.1
    vector: float = 0.3

    def normalised(self) -> ScoreWeights:
        """Rescale so the active components still sum to 1.

        Vector scoring is only available when embeddings exist; re-normalising
        prevents memories from being unfairly down-weighted just because the
        embedder is off.
        """
        total = self.lexical + self.recency + self.importance + self.usage
        if total <= 0:
            return ScoreWeights(0.45, 0.2, 0.2, 0.1, 0.0)
        return ScoreWeights(
            lexical=self.lexical / total,
            recency=self.recency / total,
            importance=self.importance / total,
            usage=self.usage / total,
            vector=self.vector,
        )


DEFAULT_WEIGHTS = ScoreWeights()


def lexical_score(query_tokens: Counter[str], tokens: list[str]) -> float:
    """Weighted overlap between the query and a memory, in ``[0, 1]``.

    Uses saturating term frequency (so repetition does not dominate) and length
    normalisation against the query, which is enough for short memory records
    and avoids a full BM25 index for a table that is read per task.
    """
    if not query_tokens or not tokens:
        return 0.0
    counts = Counter(tokens)
    matched = 0.0
    total = float(sum(query_tokens.values()))
    for term, weight in query_tokens.items():
        frequency = counts.get(term, 0)
        if frequency:
            matched += weight * (frequency / (frequency + 1.2))
    coverage = matched / total
    # Long memories that repeat the query should not outrank short precise ones.
    length_penalty = 1.0 / (1.0 + math.log1p(max(0, len(tokens) - 40)) * 0.15)
    return float(max(0.0, min(1.0, coverage * length_penalty)))


def recency_score(age_seconds: float, half_life_days: float) -> float:
    """Exponential decay in ``(0, 1]``; a fresh memory scores 1."""
    half_life = max(half_life_days, 0.001) * 86_400
    return float(0.5 ** (max(age_seconds, 0.0) / half_life))


def usage_score(access_count: int) -> float:
    """Diminishing returns on how often a memory was useful before."""
    return float(min(1.0, math.log1p(max(access_count, 0)) / math.log1p(20)))


def combine(
    *,
    lexical: float,
    recency: float,
    importance: float,
    usage: float,
    vector: float | None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    layer_boost: float = 0.0,
) -> float:
    normalised = weights.normalised()
    if vector is None:
        score = (
            normalised.lexical * lexical
            + normalised.recency * recency
            + normalised.importance * importance
            + normalised.usage * usage
        )
    else:
        vector_weight = normalised.vector
        remaining = max(0.0, 1.0 - vector_weight)
        score = vector_weight * vector + remaining * (
            normalised.lexical * lexical
            + normalised.recency * recency
            + normalised.importance * importance
            + normalised.usage * usage
        )
    return max(0.0, min(1.0, score + layer_boost))


def age_seconds(created_at_epoch: float, now: float | None = None) -> float:
    return max(0.0, (now if now is not None else time.time()) - created_at_epoch)
