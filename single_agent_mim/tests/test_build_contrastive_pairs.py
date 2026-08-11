from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_contrastive_pairs.py"
SPEC = importlib.util.spec_from_file_location("build_contrastive_pairs", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _row(label: str) -> dict:
    return {"label": label}


def test_iteration_cases_include_w2w_but_not_c2c_rows():
    result = MODULE.build_pairs(
        before={
            "c2w": _row("C"),
            "w2c": _row("W"),
            "w2w": _row("W"),
            "c2c": _row("C"),
        },
        after={
            "c2w": _row("W"),
            "w2c": _row("C"),
            "w2w": _row("W"),
            "c2c": _row("C"),
        },
        chain_id="b0_to_b1",
        from_bank="b0",
        to_bank="b1",
        from_run="empty_train",
        to_run="bank1_train",
        prior_failure_ages={"w2w": 2},
    )

    chain = result["b0_to_b1"]
    assert result["schema_version"] == "iteration_cases_v3"
    assert [row["qa_id"] for row in chain["C2W"]] == ["c2w"]
    assert [row["qa_id"] for row in chain["W2C"]] == ["w2c"]
    assert chain["W2W"] == [{
        "qa_id": "w2w",
        "before_run": "empty_train",
        "after_run": "bank1_train",
        "failure_age": 3,
    }]
    assert "C2C" not in chain
    assert chain["summary"]["C2C"] == 1
    assert chain["summary"]["learnable_cases"] == 3
