"""Regression tests against the official LoCoMo QA scoring branches."""

from mim.eval.metrics import compute_f1, normalize


def test_normalization_removes_articles_and_conjunction_and_stems():
    assert normalize("The cats, and a dog!") == "cats dog"
    assert compute_f1("running", "runs") == 1.0


def test_duplicate_tokens_use_counter_not_sets():
    assert compute_f1("cat cat", "cat") == 2.0 / 3.0


def test_category_one_scores_each_comma_separated_subanswer():
    assert compute_f1("Paris, cycling", "Paris, cycling", category=1) == 1.0


def test_category_three_ignores_reference_after_semicolon():
    assert compute_f1("Seattle", "Seattle; because she moved", category=3) == 1.0


def test_category_five_uses_official_unanswerable_phrases():
    assert compute_f1("No information available.", "anything", category=5) == 1.0
    assert compute_f1("I cannot infer that.", "anything", category=5) == 0.0
