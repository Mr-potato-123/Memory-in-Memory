"""Vector BLOB encode/decode for SQLite storage.

Format: float32 little-endian byte array.
"""

from __future__ import annotations

import numpy as np


def encode_vector(vector: np.ndarray) -> bytes:
    """Encode a float32 NumPy array to BLOB."""
    if vector.dtype != np.float32:
        vector = vector.astype(np.float32)
    return vector.tobytes()


def decode_vector(blob: bytes, dim: int | None = None) -> np.ndarray:
    """Decode a BLOB back to a float32 NumPy array."""
    vec = np.frombuffer(blob, dtype=np.float32)
    if dim is not None and len(vec) != dim:
        raise ValueError(
            f"Expected embedding dimension {dim}, got {len(vec)}"
        )
    return vec


def decode_vectors(blobs: list[bytes], dim: int) -> np.ndarray:
    """Decode multiple BLOBs into a (N, dim) float32 matrix."""
    if not blobs:
        return np.empty((0, dim), dtype=np.float32)
    matrix = np.empty((len(blobs), dim), dtype=np.float32)
    for i, blob in enumerate(blobs):
        matrix[i] = decode_vector(blob, dim)
    return matrix
