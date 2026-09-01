from __future__ import annotations

from pathlib import Path

import pytest

from zq_arb.config import Settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(_env_file=Path(".env.example"))
