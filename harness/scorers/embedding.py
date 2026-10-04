"""Embedding similarity scorer: cosine similarity between sentence embeddings.

Where exact/regex ask "is the text right?", this asks "does the text *mean*
the right thing?" -- "Paris" vs "The capital is Paris" scores near 1.0.

The embedding function is injectable so tests (and alternative backends) don't
need sentence-transformers installed; by default the model is lazy-loaded on
first score() call. Install with: pip install .[embedding]
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Sequence
from numbers import Real

# An EmbedFn maps a list of texts to a list of same-length numeric vectors.
EmbedFn = Callable[[list[str]], Sequence[Sequence[float]]]

DEFAULT_MODEL = "all-MiniLM-L6-v2"


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or len(a) == 0:
        raise ValueError("embedding vectors must have the same nonzero dimension")

    def scaled(vector: Sequence[float]) -> list[float]:
        values = []
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, Real):
                raise ValueError("embedding vectors must contain finite real numbers")
            try:
                value = float(value)
            except (OverflowError, ValueError) as exc:
                raise ValueError("embedding vectors must contain finite real numbers") from exc
            if not math.isfinite(value):
                raise ValueError("embedding vectors must contain finite real numbers")
            values.append(value)
        scale = max(abs(value) for value in values)
        # Cosine is invariant to positive scaling. Rescaling prevents valid
        # very large/small components from overflowing/underflowing norms.
        return [value / scale for value in values] if scale else values

    left, right = scaled(a), scaled(b)
    norm_a = math.sqrt(math.fsum(value * value for value in left))
    norm_b = math.sqrt(math.fsum(value * value for value in right))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    dot = math.fsum(x * y for x, y in zip(left, right, strict=True))
    similarity = dot / (norm_a * norm_b)
    if not math.isfinite(similarity):
        raise ValueError("embedding cosine similarity must be finite")
    return similarity


def _load_sentence_transformer(model_name: str) -> EmbedFn:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        raise ImportError(
            "EmbeddingScorer needs sentence-transformers; install with: pip install .[embedding]"
        ) from e

    model = SentenceTransformer(model_name)
    return lambda texts: model.encode(texts).tolist()


class EmbeddingScorer:
    name = "embedding"

    def __init__(self, embed_fn: EmbedFn | None = None, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self._embed = embed_fn
        self._init_lock = threading.Lock()

    def score(self, expected: str, actual: str) -> float:
        if self._embed is None:
            # concurrent eval threads must not each load their own model copy
            with self._init_lock:
                if self._embed is None:
                    self._embed = _load_sentence_transformer(self.model_name)
        vec_expected, vec_actual = self._embed([expected, actual])
        # Cosine ranges [-1, 1]; negative similarity is "completely wrong",
        # so clamp into the scorer contract's [0, 1].
        return max(0.0, min(1.0, cosine_similarity(vec_expected, vec_actual)))
