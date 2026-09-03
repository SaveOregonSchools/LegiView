"""Small, dependency-free helpers for strong whole-file hashing."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO


DEFAULT_HASH_CHUNK_SIZE = 8 * 1024 * 1024


def sha256_stream(handle: BinaryIO, *, chunk_size: int = DEFAULT_HASH_CHUNK_SIZE) -> str:
    """Return the SHA-256 digest of a binary stream from its current position."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(chunk_size), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = DEFAULT_HASH_CHUNK_SIZE) -> str:
    """Return a lower-case SHA-256 digest without loading the file into memory."""

    source = Path(path)
    with source.open("rb") as handle:
        return sha256_stream(handle, chunk_size=chunk_size)


def hash_file(
    path: str | Path,
    algorithm: str = "sha256",
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> str:
    """Compatibility wrapper used by collection and recovery services."""

    if algorithm.casefold() != "sha256":
        raise ValueError(f"Unsupported hash algorithm: {algorithm}")
    return sha256_file(path, chunk_size=chunk_size)


__all__ = [
    "DEFAULT_HASH_CHUNK_SIZE",
    "hash_file",
    "sha256_file",
    "sha256_stream",
]
