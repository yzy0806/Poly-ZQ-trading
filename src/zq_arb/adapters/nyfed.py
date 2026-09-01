from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from zq_arb.config import Settings
from zq_arb.domain.models import EffrObservation, utc_now


class NewYorkFedProtocolError(RuntimeError):
    """The official response could not be converted into one validated EFFR observation."""


def _decimal(value: Any, *, field: str, required: bool = False) -> Decimal | None:
    if value is None or value == "":
        if required:
            raise NewYorkFedProtocolError(f"New York Fed EFFR response is missing {field}")
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NewYorkFedProtocolError(
            f"New York Fed EFFR response has invalid {field}"
        ) from exc
    if not number.is_finite():
        raise NewYorkFedProtocolError(f"New York Fed EFFR response has non-finite {field}")
    return number


class NewYorkFedEffrAdapter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.nyfed_effr_timeout_seconds),
            follow_redirects=True,
        )

    async def fetch_latest(self) -> EffrObservation:
        response = await self._http.get(self.settings.nyfed_effr_api_url)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise NewYorkFedProtocolError("New York Fed EFFR response is not valid JSON") from exc
        rows = payload.get("refRates") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise NewYorkFedProtocolError("New York Fed EFFR response has no refRates list")
        matches = [row for row in rows if isinstance(row, dict) and row.get("type") == "EFFR"]
        if len(matches) != 1:
            raise NewYorkFedProtocolError(
                f"New York Fed EFFR response contains {len(matches)} EFFR rows"
            )
        row = matches[0]
        rate = _decimal(row.get("percentRate"), field="percentRate", required=True)
        assert rate is not None
        if not Decimal("0") <= rate <= Decimal("20"):
            raise NewYorkFedProtocolError("New York Fed EFFR percentRate is outside 0-20%")
        try:
            effective_date = date.fromisoformat(str(row.get("effectiveDate") or ""))
        except ValueError as exc:
            raise NewYorkFedProtocolError(
                "New York Fed EFFR response has invalid effectiveDate"
            ) from exc
        fetched_at = utc_now()
        calendar_age = (fetched_at.date() - effective_date).days
        if calendar_age < 0:
            raise NewYorkFedProtocolError("New York Fed EFFR effectiveDate is in the future")
        if calendar_age > self.settings.nyfed_effr_max_age_days:
            raise NewYorkFedProtocolError(
                f"New York Fed EFFR is {calendar_age} calendar days old; "
                f"maximum is {self.settings.nyfed_effr_max_age_days}"
            )
        return EffrObservation(
            source="NYFED_API",
            rate_percent=rate,
            effective_date=effective_date,
            fetched_at=fetched_at,
            target_rate_from=_decimal(row.get("targetRateFrom"), field="targetRateFrom"),
            target_rate_to=_decimal(row.get("targetRateTo"), field="targetRateTo"),
            revision_indicator=str(row.get("revisionIndicator") or ""),
            valid=True,
            reason=(
                f"New York Fed official EFFR effective {effective_date.isoformat()} "
                f"({calendar_age} calendar days old)"
            ),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()
