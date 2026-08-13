"""Configurable embedding model wrapper.

Supports:
  - sentence-transformers models (e.g., all-MiniLM-L6-v2)
  - Qwen3-Embedding models
  - Configurable device, batch size, normalization
"""

from __future__ import annotations

import hashlib
import threading
import numpy as np


class Embedder:
    """Text → float32 vector via sentence-transformers."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        device: str = "cpu",
        normalize: bool = True,
        batch_size: int = 32,
    ):
        self._model_name = model_name
        self._device = device
        self._normalize = normalize
        self._batch_size = batch_size
        self._model = None
        self._dim: int | None = None
        self._load_lock = threading.Lock()
        self._hash_fallback = model_name in {"hash", "mock", "deterministic-hash"}
        self._load_error: str | None = None

    def _ensure_model(self):
        if self._model is not None or self._hash_fallback:
            if self._dim is None:
                self._dim = 384
            return
        # Full runs share one Embedder across conversation workers.  Guard the
        # lazy initializer so those workers do not load six transformer copies
        # concurrently on their first store access.
        with self._load_lock:
            if self._model is not None or self._hash_fallback:
                if self._dim is None:
                    self._dim = 384
                return
            try:
                from sentence_transformers import SentenceTransformer
                try:
                    self._model = SentenceTransformer(
                        self._model_name, device=self._device,
                        trust_remote_code=True, local_files_only=True,
                    )
                except (OSError, ValueError):
                    self._model = SentenceTransformer(
                        self._model_name, device=self._device,
                        trust_remote_code=True,
                    )
                test_vec = self._model.encode(["test"], show_progress_bar=False)
                self._dim = test_vec.shape[1]
            except (ImportError, OSError) as exc:
                self._load_error = str(exc)
                raise RuntimeError(
                    f"Failed to load embedding model {self._model_name!r}; "
                    "refusing to substitute an unrelated hash embedding."
                ) from exc

    @property
    def dim(self) -> int:
        self._ensure_model()
        return self._dim  # type: ignore[return-value]

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def backend(self) -> str:
        """Return the effective backend after forcing model initialization."""
        self._ensure_model()
        return "deterministic-hash" if self._hash_fallback else "sentence-transformers"

    @property
    def load_error(self) -> str | None:
        self._ensure_model()
        return self._load_error

    def encode(self, texts: list[str]) -> np.ndarray:
        """Encode texts → (N, dim) float32 array."""
        self._ensure_model()
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)
        if self._hash_fallback:
            return np.vstack([self._hash_encode(text) for text in texts])
        vecs = self._model.encode(
            texts,
            batch_size=self._batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=self._normalize,
        )
        if vecs.dtype != np.float32:
            vecs = vecs.astype(np.float32)
        return vecs  # type: ignore[return-value]

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        """Encode retrieval queries, using a model's query prompt when present."""
        self._ensure_model()
        if not texts or self._hash_fallback:
            return self.encode(texts)
        encode_query = getattr(self._model, "encode_query", None)
        if callable(encode_query):
            vecs = encode_query(
                texts,
                batch_size=self._batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=self._normalize,
            )
            return vecs.astype(np.float32, copy=False)
        return self.encode(texts)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        """Encode stored memories with the document side of asymmetric models."""
        self._ensure_model()
        if not texts or self._hash_fallback:
            return self.encode(texts)
        encode_document = getattr(self._model, "encode_document", None)
        if callable(encode_document):
            vecs = encode_document(
                texts,
                batch_size=self._batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=self._normalize,
            )
            return vecs.astype(np.float32, copy=False)
        return self.encode(texts)

    def _hash_encode(self, text: str) -> np.ndarray:
        """Dependency-free deterministic embedding used by tests and smoke."""
        vec = np.zeros(self.dim, dtype=np.float32)
        tokens = text.lower().split()
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[index] += sign
        if self._normalize:
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec /= norm
        return vec
