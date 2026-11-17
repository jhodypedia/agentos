"""Memory subsystem: scoring, embeddings and sensitive-data protection."""

from app.memory.embeddings import Embedder, EmbeddingError, cosine_similarity, resolve_embedder
from app.memory.scoring import DEFAULT_WEIGHTS, ScoreWeights, combine, lexical_score, tokenize
from app.memory.sensitivity import SensitivityVerdict, inspect, redact

__all__ = [
    "DEFAULT_WEIGHTS",
    "Embedder",
    "EmbeddingError",
    "ScoreWeights",
    "SensitivityVerdict",
    "combine",
    "cosine_similarity",
    "inspect",
    "lexical_score",
    "redact",
    "resolve_embedder",
    "tokenize",
]
