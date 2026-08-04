"""Official LoCoMo question-answering normalization and F1."""

from __future__ import annotations

from collections import Counter
import re
import string

from nltk.stem import PorterStemmer

_STEMMER = PorterStemmer()


def normalize(text: str) -> str:
    """Match ``LoCoMo/task_eval/evaluation.py::normalize_answer``."""
    text = str(text).replace(",", "").lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\b(a|an|the|and)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def token_f1(pred_tokens: list[str], ref_tokens: list[str]) -> float:
    """Counter-based token F1, preserving duplicate-token counts."""
    if not pred_tokens and not ref_tokens:
        return 1.0
    if not pred_tokens or not ref_tokens:
        return 0.0
    true_positives = sum((Counter(pred_tokens) & Counter(ref_tokens)).values())
    if true_positives == 0:
        return 0.0
    precision = true_positives / len(pred_tokens)
    recall = true_positives / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


def _single_f1(prediction: str, reference: str) -> float:
    pred_tokens = [_STEMMER.stem(word) for word in normalize(prediction).split()]
    ref_tokens = [_STEMMER.stem(word) for word in normalize(reference).split()]
    return token_f1(pred_tokens, ref_tokens)


def compute_f1(
    prediction: str,
    reference: str,
    category: int | None = None,
) -> float:
    """Compute the official category-aware LoCoMo QA score.

    Category 1 is multi-hop, 2 temporal, 3 open-domain, 4 single-hop,
    and 5 adversarial. When category is omitted, use standard single-answer
    F1 so training and diagnostic callers remain backward compatible.
    """
    if category == 5:
        lowered = str(prediction).lower()
        return float(
            "no information available" in lowered
            or "not mentioned" in lowered
        )
    if category == 3:
        reference = str(reference).split(";")[0].strip()
    if category == 1:
        predictions = [part.strip() for part in str(prediction).split(",")]
        references = [part.strip() for part in str(reference).split(",")]
        if not references:
            return 0.0
        return sum(
            max((_single_f1(pred, ref) for pred in predictions), default=0.0)
            for ref in references
        ) / len(references)
    return _single_f1(str(prediction), str(reference))


def aggregate_metrics(results: list[dict]) -> dict:
    """Aggregate precomputed per-QA F1 values."""
    if not results:
        return {"overall_f1": 0.0, "count": 0, "category_f1": {}}

    overall = sum(item["f1"] for item in results) / len(results)
    category_scores: dict[int, list[float]] = {}
    for item in results:
        category = item.get("category")
        if category is not None:
            category_scores.setdefault(int(category), []).append(item["f1"])

    return {
        "overall_f1": overall,
        "count": len(results),
        "category_f1": {
            category: sum(scores) / len(scores)
            for category, scores in category_scores.items()
        },
    }
