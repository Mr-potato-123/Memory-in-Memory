"""Precompute the document-side embedding index for a published Skill Bank.

The resulting ``skill_embeddings.npz`` lives beside the published Access and
Construction JSON files.  Runtime retrieval validates the model and a hash of
the active Skill texts before using it, so publishing a new bank cannot reuse
an incompatible index accidentally.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mim.config import load_config
from mim.retrieval.embedder import Embedder
from mim.skills import SkillBank


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--bank-dir", required=True, nargs="+")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    embedder = Embedder(
        model_name=config.embedding.model,
        device=config.embedding.device,
        normalize=config.embedding.normalize,
        batch_size=config.embedding.batch_size,
    )
    for raw_bank_dir in args.bank_dir:
        bank_dir = Path(raw_bank_dir)
        bank = SkillBank.load_published(bank_dir)
        cache_path = bank.precompute_embeddings(embedder, force=args.force)
        with np.load(cache_path, allow_pickle=False) as payload:
            metadata = json.loads(str(payload["metadata"].item()))
            shape = list(payload["vectors"].shape)
        print(
            json.dumps(
                {
                    "bank_dir": str(bank_dir),
                    "cache": str(cache_path),
                    "shape": shape,
                    **metadata,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
