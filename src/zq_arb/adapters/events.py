from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from zq_arb.domain.models import utc_now


class VenueEvent(BaseModel):
    """Small, immutable boundary object shared by both venue adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    venue: Literal["IBKR", "POLYMARKET"]
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_timestamp: datetime | None = None
    received_at: datetime = Field(default_factory=utc_now)
