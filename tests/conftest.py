from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from zq_arb.config import Settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    configured = Settings(_env_file=Path(".env.example"))
    return configured.model_copy(
        update={
            "effr_source": "MANUAL",
            "pre_meeting_effr_percent": Decimal("3.625"),
        }
    )
