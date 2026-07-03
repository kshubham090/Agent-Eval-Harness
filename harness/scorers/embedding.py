"""Embedding similarity scorer: cosine similarity between sentence embeddings.

Where exact/regex ask "is the text right?", this asks "does the text *mean*
the right thing?" -- "Paris" vs "The capital is Paris" scores near 1.0.

The embedding function is injectable so tests (and alternative backends) don't
need sentence-transformers installed; by default the model is lazy-loaded on
first score() call. Install with: pip install .[embedding]
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

# An EmbedFn maps a list of texts to a list of same-length numeric vectors.
EmbedFn = Callable[[list[str]], Sequence[Sequence[float]]]

DEFAULT_MODEL = "all-MiniLM-L6-v2"


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


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

    def score(self, expected: str, actual: str) -> float:
        if self._embed is None:
            self._embed = _load_sentence_transformer(self.model_name)
        vec_expected, vec_actual = self._embed([expected, actual])
        # Cosine ranges [-1, 1]; negative similarity is "completely wrong",
        # so clamp into the scorer contract's [0, 1].
        return max(0.0, min(1.0, cosine_similarity(vec_expected, vec_actual)))
