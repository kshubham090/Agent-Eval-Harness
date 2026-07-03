import pytest

from harness.scorers import EmbeddingScorer, ExactMatchScorer, RegexScorer, Scorer
from harness.scorers.embedding import cosine_similarity

# --- exact match ---


def test_exact_match_scores_one_on_identical():
    assert ExactMatchScorer().score("Paris", "Paris") == 1.0


def test_exact_match_scores_zero_on_different():
    assert ExactMatchScorer().score("Paris", "London") == 0.0


def test_exact_match_ignores_surrounding_whitespace():
    assert ExactMatchScorer().score("Paris", "  Paris \n") == 1.0


def test_exact_match_is_case_sensitive_by_default():
    assert ExactMatchScorer().score("Paris", "paris") == 0.0


def test_exact_match_case_insensitive_option():
    assert ExactMatchScorer(case_sensitive=False).score("Paris", "PARIS") == 1.0


# --- regex ---


def test_regex_searches_anywhere_by_default():
    assert RegexScorer().score(r"(?i)paris", "The answer is Paris.") == 1.0


def test_regex_scores_zero_on_no_match():
    assert RegexScorer().score(r"(?i)paris", "London") == 0.0


def test_regex_full_match_requires_whole_output():
    assert RegexScorer(full_match=True).score(r"Paris", "The answer is Paris") == 0.0
    assert RegexScorer(full_match=True).score(r".*Paris", "The answer is Paris") == 1.0


def test_regex_matches_shapes_not_just_words():
    date_pattern = r"\d{4}-\d{2}-\d{2}"
    assert RegexScorer().score(date_pattern, "Delivery on 2026-07-03.") == 1.0
    assert RegexScorer().score(date_pattern, "Delivery on July 3rd.") == 0.0


def test_regex_rejects_invalid_pattern():
    with pytest.raises(ValueError, match="invalid regex"):
        RegexScorer().score("(unclosed", "anything")


# --- embedding ---


def fake_embedder(vectors: dict[str, list[float]]):
    return lambda texts: [vectors[t] for t in texts]


def test_embedding_identical_meaning_scores_one():
    embed = fake_embedder({"Paris": [1.0, 0.0], "paris is the capital": [1.0, 0.0]})
    scorer = EmbeddingScorer(embed_fn=embed)
    assert scorer.score("Paris", "paris is the capital") == pytest.approx(1.0)


def test_embedding_orthogonal_meaning_scores_zero():
    embed = fake_embedder({"Paris": [1.0, 0.0], "banana": [0.0, 1.0]})
    assert EmbeddingScorer(embed_fn=embed).score("Paris", "banana") == pytest.approx(0.0)


def test_embedding_clamps_negative_cosine_to_zero():
    embed = fake_embedder({"hot": [1.0, 0.0], "cold": [-1.0, 0.0]})
    assert EmbeddingScorer(embed_fn=embed).score("hot", "cold") == 0.0


def test_embedding_partial_similarity_lands_between():
    embed = fake_embedder({"a": [1.0, 0.0], "b": [1.0, 1.0]})
    score = EmbeddingScorer(embed_fn=embed).score("a", "b")
    assert 0.0 < score < 1.0


def test_cosine_of_zero_vector_is_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_embedding_without_backend_raises_helpful_error():
    try:
        import sentence_transformers  # noqa: F401

        pytest.skip("sentence-transformers is installed")
    except ImportError:
        pass
    with pytest.raises(ImportError, match="pip install"):
        EmbeddingScorer().score("a", "b")


@pytest.mark.slow
def test_embedding_with_real_model():
    pytest.importorskip("sentence_transformers")
    scorer = EmbeddingScorer()
    same = scorer.score("Paris", "The capital of France is Paris")
    different = scorer.score("Paris", "Bananas are yellow")
    assert same > different
    assert same > 0.5


# --- protocol ---


@pytest.mark.parametrize(
    "scorer",
    [ExactMatchScorer(), RegexScorer(), EmbeddingScorer(embed_fn=lambda t: [[1.0]] * len(t))],
)
def test_scorers_satisfy_protocol(scorer):
    assert isinstance(scorer, Scorer)
