from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from zq_arb.adapters.nyfed import NewYorkFedEffrAdapter, NewYorkFedProtocolError
from zq_arb.config import Settings
from zq_arb.domain.models import utc_now


def response_payload(*, age_days: int = 1) -> dict[str, object]:
    effective_date = (utc_now().date() - timedelta(days=age_days)).isoformat()
    return {
        "refRates": [
            {"type": "SOFR", "effectiveDate": effective_date, "percentRate": 3.65},
            {
                "type": "EFFR",
                "effectiveDate": effective_date,
                "percentRate": 3.63,
                "targetRateFrom": 3.50,
                "targetRateTo": 3.75,
                "revisionIndicator": "",
            },
        ]
    }


@pytest.mark.asyncio
async def test_official_effr_response_is_typed_and_validated(settings: Settings) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response_payload(), request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = NewYorkFedEffrAdapter(settings, client)
        observation = await adapter.fetch_latest()

    assert observation.valid
    assert observation.source == "NYFED_API"
    assert str(observation.rate_percent) == "3.63"
    assert str(observation.target_rate_from) == "3.5"
    assert observation.effective_date is not None


@pytest.mark.asyncio
async def test_stale_effr_response_fails_closed(settings: Settings) -> None:
    stale_age = settings.nyfed_effr_max_age_days + 1
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=response_payload(age_days=stale_age),
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = NewYorkFedEffrAdapter(settings, client)
        with pytest.raises(NewYorkFedProtocolError, match="calendar days old"):
            await adapter.fetch_latest()


@pytest.mark.asyncio
async def test_missing_effr_row_fails_closed(settings: Settings) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"refRates": []}, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = NewYorkFedEffrAdapter(settings, client)
        with pytest.raises(NewYorkFedProtocolError, match="0 EFFR rows"):
            await adapter.fetch_latest()
