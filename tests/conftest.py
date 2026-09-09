from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from zq_arb.config import Settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    configured = Settings(_env_file=Path("deploy/zq-arb.env.example"))
    return configured.model_copy(
        update={
            "effr_source": "MANUAL",
            "cookie_secure": False,
            "polymarket_funder_address": SecretStr("0x0000000000000000000000000000000000000001"),
            "pre_meeting_effr_percent": Decimal("3.625"),
        }
    )
