from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

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


@dataclass(frozen=True, slots=True)
class PolymarketOrderResult:
    order_id: str
    status: str
    requested_shares: Decimal
    immediately_matched_shares: Decimal
    limit_price: Decimal
    simulated: bool = False


@dataclass(frozen=True, slots=True)
class PolymarketAccountSnapshot:
    open_orders: tuple[dict[str, Any], ...]
    trades: tuple[dict[str, Any], ...]
    captured_at: datetime
    positions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedPolymarketOrder:
    token_id: str
    limit_price: Decimal
    shares: Decimal
    idempotency_key: str
    signed_payload: dict[str, Any] | None


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
        min_order_size = _decimal(_payload_value(payload, "min_order_size", "minOrderSize"))
        negative_risk_value = _payload_value(payload, "neg_risk", "negRisk")
        book = OrderBook(
            token_id=token_id,
            market=market,
            bids=_levels(payload.get("bids"), reverse=True),
            asks=_levels(payload.get("asks"), reverse=False),
            tick_size=(
                tick_size
                if tick_size is not None
                else existing.tick_size
                if existing is not None
                else None
            ),
            min_order_size=(
                min_order_size
                if min_order_size is not None
                else existing.min_order_size
                if existing is not None
                else None
            ),
            negative_risk=(
                bool(negative_risk_value)
                if negative_risk_value is not None
                else existing.negative_risk
                if existing is not None
                else None
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
    """Public market data plus a separately gated authenticated execution path."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        timeout = httpx.Timeout(connect=10, read=20, write=10, pool=10)
        self._http = httpx.AsyncClient(timeout=timeout, http2=True)
        self._closed = False
        self._secure_client: Any | None = None
        self._secure_lock = asyncio.Lock()
        self._heartbeat_id: str | None = None

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._secure_client is not None:
                await self._secure_client.close()
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

    async def fetch_hedge_fee_parameters(self) -> dict[str, dict[str, Decimal]]:
        """Fetch current dynamic taker-fee parameters for both hedge markets."""

        result: dict[str, dict[str, Decimal]] = {}
        for leg in self.settings.market_legs:
            if leg.code not in {"INC25", "INC50PLUS"}:
                continue
            url = (
                f"{self.settings.polymarket_clob_host.rstrip('/')}/clob-markets/{leg.condition_id}"
            )
            payload = await self._get(url)
            if not isinstance(payload, Mapping):
                raise PolymarketProtocolError("CLOB market fee response is not an object")
            raw_fee = payload.get("fd")
            if raw_fee is None:
                rate = Decimal("0")
                exponent = Decimal("0")
            elif isinstance(raw_fee, Mapping):
                parsed_rate = _decimal(raw_fee.get("r"))
                parsed_exponent = _decimal(raw_fee.get("e"))
                if (
                    parsed_rate is None
                    or parsed_exponent is None
                    or parsed_rate < 0
                    or parsed_exponent < 0
                ):
                    raise PolymarketProtocolError("CLOB fee parameters are invalid")
                rate = parsed_rate
                exponent = parsed_exponent
            else:
                raise PolymarketProtocolError("CLOB fee parameters have an invalid shape")
            result[leg.code] = {"rate": rate, "exponent": exponent}
        return result

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

    def _routing_authorized(self) -> None:
        if not self.settings.polymarket_order_submission_enabled:
            raise PermissionError("POLYMARKET_ORDER_SUBMISSION_ENABLED is false")
        if self.settings.run_mode.value == "PAPER":
            return
        if self.settings.run_mode.is_live and self.settings.live_trading_enabled:
            return
        raise PermissionError("the current run mode does not authorize Polymarket orders")

    async def _authenticated_client(self) -> Any:
        if self._secure_client is not None:
            return self._secure_client
        async with self._secure_lock:
            if self._secure_client is not None:
                return self._secure_client
            if not self.settings.clob_credentials_configured:
                raise PermissionError("CLOB L2 credentials are not configured")
            private_key = self.settings.polymarket_private_key.get_secret_value()
            if not self.settings._is_configured(private_key):
                raise PermissionError("Polymarket signing key is not configured")
            try:
                from polymarket import AsyncSecureClient
                from polymarket.models import ApiKeyCreds
            except ImportError as exc:
                raise PolymarketProtocolError(
                    "official authenticated Polymarket SDK is unavailable"
                ) from exc
            credentials = ApiKeyCreds(
                key=self.settings.polymarket_api_key.get_secret_value(),
                secret=self.settings.polymarket_api_secret.get_secret_value(),
                passphrase=self.settings.polymarket_api_passphrase.get_secret_value(),
            )
            self._secure_client = await AsyncSecureClient.create(
                private_key=private_key,
                wallet=self.settings.polymarket_funder_address.get_secret_value(),
                credentials=credentials,
            )
            return self._secure_client

    async def trading_preflight(self, required_cash: Decimal) -> dict[str, Any]:
        """Verify collateral balance and at least one usable exchange allowance."""

        if required_cash < 0:
            raise ValueError("required cash cannot be negative")
        if self.settings.simulate_polymarket_fills:
            return {
                "simulated": True,
                "required_cash": str(required_cash),
                "balance_cash": None,
                "allowance_sufficient": True,
            }
        client = await self._authenticated_client()
        balance_allowance = await client.get_balance_allowance(asset_type="COLLATERAL")
        required_base_units = int((required_cash * Decimal("1000000")).to_integral_value())
        balance_ok = balance_allowance.balance >= required_base_units
        allowance_ok = any(
            value >= required_base_units for value in balance_allowance.allowances.values()
        )
        if not balance_ok or not allowance_ok:
            raise PermissionError(
                "Polymarket collateral balance or exchange allowance is below the hedge reserve"
            )
        return {
            "simulated": False,
            "required_cash": str(required_cash),
            "balance_cash": str(Decimal(balance_allowance.balance) / Decimal("1000000")),
            "allowance_sufficient": allowance_ok,
        }

    async def submit_hedge_limit(
        self,
        *,
        token_id: str,
        limit_price: Decimal,
        shares: Decimal,
        idempotency_key: str,
    ) -> PolymarketOrderResult:
        """Submit a non-post-only GTC BUY limit at the coordinator-supplied lowest ask."""

        prepared = await self.prepare_hedge_limit(
            token_id=token_id,
            limit_price=limit_price,
            shares=shares,
            idempotency_key=idempotency_key,
        )
        return await self.post_prepared_hedge(prepared)

    async def prepare_hedge_limit(
        self,
        *,
        token_id: str,
        limit_price: Decimal,
        shares: Decimal,
        idempotency_key: str,
    ) -> PreparedPolymarketOrder:
        """Sign once so the exact order payload can be durably stored before POST."""

        self._routing_authorized()
        if shares <= 0:
            raise ValueError("hedge shares must be positive")
        if limit_price <= 0 or limit_price > self.settings.polymarket_emergency_max_price:
            raise ValueError("hedge limit price is outside the emergency cap")
        if self.settings.polymarket_post_only:
            raise RuntimeError("hedge routing refuses post-only configuration")
        if self.settings.simulate_polymarket_fills:
            return PreparedPolymarketOrder(
                token_id=token_id,
                limit_price=limit_price,
                shares=shares,
                idempotency_key=idempotency_key,
                signed_payload=None,
            )
        client = await self._authenticated_client()
        signed_order = await client.create_limit_order(
            token_id=token_id,
            price=limit_price,
            size=shares,
            side="BUY",
            post_only=False,
        )
        return PreparedPolymarketOrder(
            token_id=token_id,
            limit_price=limit_price,
            shares=shares,
            idempotency_key=idempotency_key,
            signed_payload=asdict(signed_order),
        )

    async def post_prepared_hedge(self, prepared: PreparedPolymarketOrder) -> PolymarketOrderResult:
        """POST a previously persisted signed order; retries keep the same order hash."""

        self._routing_authorized()
        if prepared.signed_payload is None:
            digest = hashlib.sha256(prepared.idempotency_key.encode("utf-8")).hexdigest()[:32]
            return PolymarketOrderResult(
                order_id=f"SIM-{digest}",
                status="matched",
                requested_shares=prepared.shares,
                immediately_matched_shares=prepared.shares,
                limit_price=prepared.limit_price,
                simulated=True,
            )
        client = await self._authenticated_client()
        from polymarket.models.clob.orders import SignedOrder

        signed_order = SignedOrder(**prepared.signed_payload)
        response = await client.post_order(signed_order)
        if not response.ok:
            raise PolymarketProtocolError(
                f"Polymarket rejected hedge order ({response.code}): {response.message}"
            )
        return PolymarketOrderResult(
            order_id=str(response.order_id),
            status=response.status,
            requested_shares=prepared.shares,
            # Real fills are credited only from unique user-stream/REST trade ids;
            # treating this aggregate response as another execution would double count.
            immediately_matched_shares=Decimal("0"),
            limit_price=prepared.limit_price,
        )

    async def cancel_order(self, order_id: str) -> bool:
        self._routing_authorized()
        if order_id.startswith("SIM-"):
            return True
        client = await self._authenticated_client()
        response = await client.cancel_order(order_id=order_id)
        return order_id in {str(value) for value in response.canceled}

    async def authenticated_user_stream(self) -> AsyncIterator[VenueEvent]:
        """Yield authenticated order/trade lifecycle events from the official SDK."""

        if self.settings.simulate_polymarket_fills:
            return
        client = await self._authenticated_client()
        from polymarket.streams import UserSpec

        markets = [
            leg.condition_id
            for leg in self.settings.market_legs
            if leg.code in {"INC25", "INC50PLUS"}
        ]
        async with await client.subscribe(UserSpec(markets=markets)) as stream:
            yield VenueEvent(
                venue="POLYMARKET",
                kind="user_stream_connected",
                payload={"market_count": len(markets)},
            )
            async for event in stream:
                payload = event.payload.model_dump(mode="json")
                yield VenueEvent(
                    venue="POLYMARKET",
                    kind=f"user_{event.type}",
                    payload=payload,
                    source_timestamp=_timestamp(payload.get("timestamp")),
                )

    async def account_snapshot(self, *, trade_limit: int = 500) -> PolymarketAccountSnapshot:
        client = await self._authenticated_client()
        open_orders = [
            item.model_dump(mode="json") async for item in client.list_open_orders().iter_items()
        ]
        trades: list[dict[str, Any]] = []
        async for item in client.list_account_trades().iter_items():
            trades.append(item.model_dump(mode="json"))
            if len(trades) >= trade_limit:
                break
        positions = await self.current_event_positions()
        return PolymarketAccountSnapshot(
            open_orders=tuple(open_orders),
            trades=tuple(trades),
            captured_at=utc_now(),
            positions=positions,
        )

    async def current_event_positions(self) -> tuple[dict[str, Any], ...]:
        """Fetch venue-reported wallet positions for only the configured FOMC event."""

        address = self.settings.polymarket_funder_address.get_secret_value()
        if not address:
            return ()
        url = f"{self.settings.polymarket_data_api_host.rstrip('/')}/positions"
        params = {
            "user": address,
            "market": ",".join(leg.condition_id for leg in self.settings.market_legs),
            "sizeThreshold": "0",
            "limit": "500",
        }
        payload = await self._get(url, params=params)
        if not isinstance(payload, list) or any(not isinstance(item, Mapping) for item in payload):
            raise PolymarketProtocolError("positions response is not a list of objects")
        approved_tokens = {
            token_id
            for leg in self.settings.market_legs
            for token_id in (leg.yes_token_id, leg.no_token_id)
        }
        return tuple(
            dict(item)
            for item in payload
            if str(item.get("asset") or "") in approved_tokens
        )

    async def send_order_heartbeat(self) -> str:
        """Maintain Polymarket's cancel-on-disconnect heartbeat for open orders."""

        client = await self._authenticated_client()
        payload = {"heartbeat_id": self._heartbeat_id or ""}
        # The current SDK exposes authenticated transport but not the documented
        # /heartbeats endpoint. Keep that narrow compatibility seam here.
        response = await client._ctx.secure_clob.post_json("/heartbeats", json=payload)
        if not isinstance(response, Mapping):
            raise PolymarketProtocolError("heartbeat response is not an object")
        heartbeat_id = str(response.get("heartbeat_id") or response.get("heartbeatId") or "")
        if not heartbeat_id:
            heartbeat_id = self._heartbeat_id or str(uuid4())
        self._heartbeat_id = heartbeat_id
        return heartbeat_id
