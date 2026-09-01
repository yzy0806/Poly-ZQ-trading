from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

from zq_arb.adapters.events import VenueEvent
from zq_arb.config import MarketLegConfig, Settings
from zq_arb.domain.models import (
    BookLevel,
    EligibilityStatus,
    MarketMappingStatus,
    OrderBook,
    utc_now,
)

LOGGER = structlog.get_logger(__name__)


class PolymarketProtocolError(RuntimeError):
    pass


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _decimal(value: Any, *, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1_000
        return datetime.fromtimestamp(number, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
    return None


def _levels(values: Any, *, reverse: bool) -> tuple[BookLevel, ...]:
    result: list[BookLevel] = []
    for item in values if isinstance(values, Sequence) and not isinstance(values, str) else []:
        if not isinstance(item, Mapping):
            continue
        price = _decimal(item.get("price"))
        size = _decimal(item.get("size"))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        result.append(BookLevel(price=price, size=size))
    result.sort(key=lambda level: level.price, reverse=reverse)
    return tuple(result)


def _payload_value(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return None


def _stream_token_id(payload: Mapping[str, Any]) -> str:
    return str(_payload_value(payload, "token_id", "tokenId", "asset_id", "assetId") or "")


def _validate_book(book: OrderBook) -> OrderBook:
    if any(
        level.price <= 0 or level.price >= 1 or level.size <= 0
        for level in (*book.bids, *book.asks)
    ):
        raise PolymarketProtocolError("market stream contains an invalid price or size")
    if book.best_bid is not None and book.best_ask is not None and book.best_bid >= book.best_ask:
        raise PolymarketProtocolError("market stream produced a crossed order book")
    return book


def update_books_from_stream_event(
    books: Mapping[str, OrderBook],
    event: VenueEvent,
) -> tuple[OrderBook, ...]:
    """Apply official market-stream snapshots and deltas without a REST round trip."""

    kind = event.kind.lower()
    payload = event.payload
    source_timestamp = event.source_timestamp or _timestamp(payload.get("timestamp"))
    if kind == "book":
        token_id = _stream_token_id(payload)
        if not token_id:
            raise PolymarketProtocolError("market book event has no token id")
        existing = books.get(token_id)
        market = (
            existing.market if existing is not None else str(payload.get("market") or "") or None
        )
        tick_size = _decimal(_payload_value(payload, "tick_size", "tickSize"))
        min_order_size = _decimal(
            _payload_value(payload, "min_order_size", "minOrderSize")
        )
        negative_risk_value = _payload_value(payload, "neg_risk", "negRisk")
        book = OrderBook(
            token_id=token_id,
            market=market,
            bids=_levels(payload.get("bids"), reverse=True),
            asks=_levels(payload.get("asks"), reverse=False),
            tick_size=(
                tick_size
                if tick_size is not None
                else existing.tick_size if existing is not None else None
            ),
            min_order_size=(
                min_order_size
                if min_order_size is not None
                else existing.min_order_size if existing is not None else None
            ),
            negative_risk=(
                bool(negative_risk_value)
                if negative_risk_value is not None
                else existing.negative_risk if existing is not None else None
            ),
            book_hash=str(payload.get("hash") or "") or None,
            source="WEBSOCKET",
            stream_synchronized=True,
            source_timestamp=source_timestamp,
            last_reconciled_at=existing.last_reconciled_at if existing is not None else None,
            received_at=event.received_at,
        )
        return (_validate_book(book),)

    if kind == "price_change":
        raw_changes = _payload_value(payload, "price_changes", "priceChanges")
        if not isinstance(raw_changes, Sequence) or isinstance(raw_changes, str):
            raise PolymarketProtocolError("price-change event has no changes")
        updated: dict[str, OrderBook] = {}
        for raw_change in raw_changes:
            if not isinstance(raw_change, Mapping):
                continue
            token_id = _stream_token_id(raw_change)
            current = updated.get(token_id) or books.get(token_id)
            if not token_id or current is None:
                raise PolymarketProtocolError(
                    "price change arrived before a complete book snapshot"
                )
            price = _decimal(raw_change.get("price"))
            size = _decimal(raw_change.get("size"))
            side = str(raw_change.get("side") or "").upper()
            if price is None or size is None or price <= 0 or price >= 1 or size < 0:
                raise PolymarketProtocolError("price change contains an invalid price or size")
            if side not in {"BUY", "SELL"}:
                raise PolymarketProtocolError("price change contains an invalid side")
            levels = {
                level.price: level.size
                for level in (current.bids if side == "BUY" else current.asks)
            }
            if size == 0:
                levels.pop(price, None)
            else:
                levels[price] = size
            rebuilt = tuple(
                BookLevel(price=level_price, size=level_size)
                for level_price, level_size in sorted(
                    levels.items(), key=lambda item: item[0], reverse=side == "BUY"
                )
            )
            change_hash = str(raw_change.get("hash") or "") or current.book_hash
            book_update = {
                "bids" if side == "BUY" else "asks": rebuilt,
                "book_hash": change_hash,
                "source": "WEBSOCKET",
                "stream_synchronized": True,
                "source_timestamp": source_timestamp,
                "received_at": event.received_at,
            }
            updated[token_id] = _validate_book(current.model_copy(update=book_update))
        return tuple(updated.values())

    if kind == "tick_size_change":
        token_id = _stream_token_id(payload)
        current = books.get(token_id)
        tick_size = _decimal(_payload_value(payload, "new_tick_size", "newTickSize"))
        if current is None or tick_size is None or tick_size <= 0:
            raise PolymarketProtocolError("tick-size change cannot be applied to the current book")
        return (
            current.model_copy(
                update={
                    "tick_size": tick_size,
                    "source": "WEBSOCKET",
                    "stream_synchronized": True,
                    "source_timestamp": source_timestamp,
                    "received_at": event.received_at,
                }
            ),
        )

    return ()


class PolymarketAdapter:
    """Public-data adapter; authenticated order flow is intentionally fail-closed."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        timeout = httpx.Timeout(connect=10, read=20, write=10, pool=10)
        self._http = httpx.AsyncClient(timeout=timeout, http2=True)
        self._closed = False

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._http.aclose()

    async def __aenter__(self) -> PolymarketAdapter:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _get(self, url: str, *, params: Mapping[str, str] | None = None) -> Any:
        response = await self._http.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def check_eligibility(self) -> EligibilityStatus:
        checked_at = utc_now()
        try:
            payload = await self._get(self.settings.polymarket_geoblock_url)
            if not isinstance(payload, Mapping):
                raise PolymarketProtocolError("geoblock response is not an object")
            blocked_raw = payload.get("blocked")
            blocked = blocked_raw if isinstance(blocked_raw, bool) else None
            country = str(payload.get("country") or payload.get("countryCode") or "") or None
            return EligibilityStatus(
                checked=True,
                blocked=blocked,
                country=country,
                permitted_for_live=blocked is False and country == "HK",
                checked_at=checked_at,
                reason="opening orders permitted"
                if blocked is False
                else "blocked or indeterminate",
            )
        except Exception as exc:
            LOGGER.warning("polymarket_geoblock_failed", error=str(exc))
            return EligibilityStatus(
                checked=True,
                blocked=None,
                checked_at=checked_at,
                reason=f"geoblock check failed: {type(exc).__name__}",
            )

    async def verify_market_mapping(self) -> MarketMappingStatus:
        errors: list[str] = []
        checked_at = utc_now()
        url = f"{self.settings.polymarket_gamma_api_host.rstrip('/')}/events"
        try:
            payload = await self._get(url, params={"slug": self.settings.polymarket_event_slug})
            events = payload if isinstance(payload, list) else []
            if len(events) != 1 or not isinstance(events[0], Mapping):
                raise PolymarketProtocolError(f"expected one Gamma event, received {len(events)}")
            event: Mapping[str, Any] = events[0]
            event_id = str(event.get("id") or "")
            if event_id != self.settings.polymarket_event_id:
                errors.append("event id differs from approved configuration")
            raw_markets = event.get("markets")
            markets: list[Mapping[str, Any]] = (
                [item for item in raw_markets if isinstance(item, Mapping)]
                if isinstance(raw_markets, list)
                else []
            )
            market_count_match = len(markets) == self.settings.polymarket_event_market_count
            if not market_count_match:
                errors.append("event market count differs from approved configuration")
            description = str(event.get("description") or "")
            rule_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()
            rule_hash_match = (
                rule_hash.lower() == self.settings.polymarket_event_rule_sha256.lower()
            )
            if not rule_hash_match:
                errors.append("event resolution-rule hash differs from approved configuration")

            by_id = {str(market.get("id")): market for market in markets}
            for leg in self.settings.market_legs:
                self._verify_leg(leg, by_id.get(leg.market_id), errors)
            return MarketMappingStatus(
                verified=not errors,
                rule_hash_match=rule_hash_match,
                market_count_match=market_count_match,
                checked_at=checked_at,
                errors=tuple(errors),
            )
        except Exception as exc:
            LOGGER.warning("polymarket_mapping_failed", error=str(exc))
            errors.append(f"mapping request failed: {type(exc).__name__}")
            return MarketMappingStatus(checked_at=checked_at, errors=tuple(errors))

    @staticmethod
    def _verify_leg(
        configured: MarketLegConfig,
        market: Mapping[str, Any] | None,
        errors: list[str],
    ) -> None:
        prefix = configured.code
        if market is None:
            errors.append(f"{prefix}: approved market id not found")
            return
        checks = {
            "slug": (str(market.get("slug") or ""), configured.slug),
            "condition id": (
                str(market.get("conditionId") or market.get("condition_id") or ""),
                configured.condition_id,
            ),
        }
        for label, (actual, expected) in checks.items():
            if actual != expected:
                errors.append(f"{prefix}: {label} mismatch")
        tokens = [str(value) for value in _json_list(market.get("clobTokenIds"))]
        outcomes = [str(value).lower() for value in _json_list(market.get("outcomes"))]
        token_map = dict(zip(outcomes, tokens, strict=False))
        if token_map.get("yes") != configured.yes_token_id:
            errors.append(f"{prefix}: YES token mismatch")
        if token_map.get("no") != configured.no_token_id:
            errors.append(f"{prefix}: NO token mismatch")
        tick = _decimal(market.get("orderPriceMinTickSize"))
        minimum = _decimal(market.get("orderMinSize"))
        if tick != configured.expected_tick_size:
            errors.append(f"{prefix}: tick size mismatch")
        if minimum != configured.expected_min_order_size:
            errors.append(f"{prefix}: minimum order size mismatch")
        if bool(market.get("closed")) or market.get("active") is False:
            errors.append(f"{prefix}: market is not active")

    async def fetch_book(self, token_id: str, *, market: str | None = None) -> OrderBook:
        url = f"{self.settings.polymarket_clob_host.rstrip('/')}/book"
        payload = await self._get(url, params={"token_id": token_id})
        if not isinstance(payload, Mapping):
            raise PolymarketProtocolError("CLOB book response is not an object")
        returned_token = str(payload.get("asset_id") or token_id)
        if returned_token != token_id:
            raise PolymarketProtocolError("CLOB book returned a different token")
        return OrderBook(
            token_id=token_id,
            market=market,
            bids=_levels(payload.get("bids"), reverse=True),
            asks=_levels(payload.get("asks"), reverse=False),
            tick_size=_decimal(payload.get("tick_size")),
            min_order_size=_decimal(payload.get("min_order_size")),
            negative_risk=bool(payload.get("neg_risk")) if "neg_risk" in payload else None,
            book_hash=str(payload.get("hash") or "") or None,
            source="REST",
            stream_synchronized=False,
            source_timestamp=_timestamp(payload.get("timestamp")),
            last_reconciled_at=utc_now(),
        )

    async def snapshot_all_books(self) -> tuple[OrderBook, ...]:
        requests: list[Any] = []
        for leg in self.settings.market_legs:
            requests.extend(
                (
                    self.fetch_book(leg.yes_token_id, market=f"{leg.code}_YES"),
                    self.fetch_book(leg.no_token_id, market=f"{leg.code}_NO"),
                )
            )
        results = await asyncio.gather(*requests, return_exceptions=True)
        books: list[OrderBook] = []
        failures: list[str] = []
        for result in results:
            if isinstance(result, BaseException):
                failures.append(type(result).__name__)
            else:
                books.append(result)
        if failures:
            raise PolymarketProtocolError(
                f"failed to fetch {len(failures)} of {len(results)} books: {', '.join(failures)}"
            )
        return tuple(books)

    async def public_market_stream(self, token_ids: Sequence[str]) -> AsyncIterator[VenueEvent]:
        """Use the official unified SDK; callers reconnect or fall back to REST snapshots."""

        try:
            from polymarket import AsyncPublicClient
            from polymarket.streams import MarketSpec
        except ImportError as exc:
            raise PolymarketProtocolError("official Polymarket stream SDK is unavailable") from exc

        client = AsyncPublicClient()
        async with await client.subscribe(
            MarketSpec(token_ids=list(token_ids), custom_feature_enabled=True)
        ) as stream:
            yield VenueEvent(
                venue="POLYMARKET",
                kind="stream_connected",
                payload={"token_count": len(token_ids)},
            )
            async for event in stream:
                payload = getattr(event, "payload", event)
                if hasattr(payload, "model_dump"):
                    event_payload = payload.model_dump(mode="json")
                elif isinstance(payload, Mapping):
                    event_payload = dict(payload)
                else:
                    event_payload = {"value": str(payload)}
                yield VenueEvent(
                    venue="POLYMARKET",
                    kind=str(getattr(event, "type", type(payload).__name__)),
                    payload=event_payload,
                    source_timestamp=_timestamp(event_payload.get("timestamp")),
                )

    async def pump_market_stream(
        self,
        token_ids: Sequence[str],
        callback: Callable[[VenueEvent], Awaitable[None]],
    ) -> None:
        async for event in self.public_market_stream(token_ids):
            await callback(event)

    async def submit_order(self, *_: object, **__: object) -> None:
        """Authenticated order submission is not authorized in the present build phase."""

        raise PermissionError("Polymarket order submission is disabled by implementation scope")
