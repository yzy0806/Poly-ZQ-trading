from __future__ import annotations

import hashlib


def identity_fingerprint(value: str) -> str:
    """Use one normalized identity format for venue callbacks and durable ledgers."""
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()
