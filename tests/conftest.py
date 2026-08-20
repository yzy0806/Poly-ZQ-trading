from __future__ import annotations

import pytest

from zq_arb.config import Settings, get_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()
