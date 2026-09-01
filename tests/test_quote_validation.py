from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from zq_arb.adapters.events import VenueEvent
from zq_arb.config import Settings
from zq_arb.domain.enums import (
    ConnectionStatus,
    DataQuality,
    FarmStatus,
    QuoteRole,
    SubscriptionStatus,
)
from zq_arb.domain.models import Quote, utc_now
from zq_arb.services.engine import EngineRuntime
from zq_arb.services.state import StateStore


def qualified_quote(*, role: QuoteRole, generation: int = 3) -> Quote:
    now = utc_now()
    return Quote(
        instrument="202608" if role is QuoteRole.ANCHOR else "202609",
        bid=Decimal("96.30"),
        ask=Decimal("96.31"),
        bid_size=Decimal("100"),
        ask_size=Decimal("120"),
        quality=DataQuality.LIVE,
        role=role,
        last_price_change_at=now - timedelta(hours=2),
        last_market_data_event_at=now - timedelta(hours=1),
        market_data_type=1,
        subscription_status=SubscriptionStatus.ACTIVE,
        subscription_generation=generation,
        farm_status=FarmStatus.CONNECTED,
    )


def test_quiet_prices_and_old_event_timestamps_do_not_expire(settings: Settings) -> None:
    runtime = EngineRuntime(settings)
    quote = qualified_quote(role=QuoteRole.TARGET)
    result = runtime._qualify_quote(quote, generation=3)
    assert result.analytics_qualified
    assert result.pretrade_qualified
    assert result.validation_reason == "current-generation live subscription qualified"


def test_generation_farm_data_type_and_complete_bbo_still_fail_closed(
    settings: Settings,
) -> None:
    runtime = EngineRuntime(settings)
    quote = qualified_quote(role=QuoteRole.TARGET)
    cases = (
        quote.model_copy(update={"subscription_generation": 2}),
        quote.model_copy(update={"farm_status": FarmStatus.DISCONNECTED}),
        quote.model_copy(update={"market_data_type": 3, "quality": DataQuality.DELAYED}),
        quote.model_copy(update={"ask": None}),
    )
    for candidate in cases:
        result = runtime._qualify_quote(candidate, generation=3)
        assert not result.analytics_qualified
        assert not result.pretrade_qualified


async def prepare_live_store(settings: Settings) -> StateStore:
    store = StateStore(settings)
    await store.begin_ibkr_subscriptions()
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 2104, "message": "Market data farm connection is OK:usfuture"},
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="market_data_type",
            payload={"request_id": 2002, "month": "202609", "market_data_type": 1},
        )
    )
    return store


@pytest.mark.asyncio
async def test_stream_requires_complete_live_uncrossed_bid_ask(settings: Settings) -> None:
    store = await prepare_live_store(settings)
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="tick_price",
            payload={"request_id": 2002, "month": "202609", "tick_type": 1, "price": "96.30"},
        )
    )
    first = (await store.get()).quotes["202609"]
    assert first.subscription_status is SubscriptionStatus.PENDING_REVALIDATION
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="tick_price",
            payload={"request_id": 2002, "month": "202609", "tick_type": 2, "price": "96.31"},
        )
    )
    complete = (await store.get()).quotes["202609"]
    assert complete.subscription_status is SubscriptionStatus.ACTIVE
    assert complete.has_valid_two_sided_market


@pytest.mark.asyncio
async def test_callback_order_does_not_leave_complete_startup_bbo_pending(
    settings: Settings,
) -> None:
    store = StateStore(settings)
    await store.begin_ibkr_subscriptions()
    for tick_type, price in ((1, "96.30"), (2, "96.31")):
        await store.apply_ibkr_event(
            VenueEvent(
                venue="IBKR",
                kind="tick_price",
                payload={
                    "request_id": 2002,
                    "month": "202609",
                    "tick_type": tick_type,
                    "price": price,
                },
            )
        )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="market_data_type",
            payload={"request_id": 2002, "month": "202609", "market_data_type": 1},
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 2104, "message": "Market data farm connection is OK:usfuture"},
        )
    )
    quote = (await store.get()).quotes["202609"]
    assert quote.subscription_status is SubscriptionStatus.ACTIVE


@pytest.mark.asyncio
async def test_bid_size_callback_refreshes_activity_without_price_change(
    settings: Settings,
) -> None:
    store = await prepare_live_store(settings)
    for tick_type, price in ((1, "96.30"), (2, "96.31")):
        await store.apply_ibkr_event(
            VenueEvent(
                venue="IBKR",
                kind="tick_price",
                payload={
                    "request_id": 2002,
                    "month": "202609",
                    "tick_type": tick_type,
                    "price": price,
                },
            )
        )
    before = (await store.get()).quotes["202609"]
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="tick_size",
            payload={"request_id": 2002, "month": "202609", "tick_type": 0, "size": "101"},
        )
    )
    after = (await store.get()).quotes["202609"]
    assert after.bid == before.bid and after.ask == before.ask
    assert after.last_price_change_at == before.last_price_change_at
    assert after.last_market_data_event_at is not None
    assert before.last_market_data_event_at is not None
    assert after.last_market_data_event_at > before.last_market_data_event_at
    assert after.bid_size == Decimal("101")
    assert after.subscription_status is SubscriptionStatus.ACTIVE


@pytest.mark.asyncio
async def test_farm_recovery_requires_resubscribe_and_new_complete_bbo(
    settings: Settings,
) -> None:
    store = await prepare_live_store(settings)
    for tick_type, price in ((1, "96.30"), (2, "96.31")):
        await store.apply_ibkr_event(
            VenueEvent(
                venue="IBKR",
                kind="tick_price",
                payload={"month": "202609", "tick_type": tick_type, "price": price},
            )
        )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 2103, "message": "Market data farm connection is broken:usfuture"},
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 2104, "message": "Market data farm connection is OK:usfuture"},
        )
    )
    recovered = await store.get()
    assert recovered.metadata["ibkr_resubscribe_required"] is True
    assert recovered.quotes["202609"].subscription_status is SubscriptionStatus.PENDING_REVALIDATION
    generation = await store.begin_ibkr_subscriptions()
    cleared = (await store.get()).quotes["202609"]
    assert cleared.bid is None and cleared.ask is None
    assert cleared.subscription_generation == generation


@pytest.mark.asyncio
async def test_hmds_and_secdef_outages_do_not_invalidate_live_zq(settings: Settings) -> None:
    store = StateStore(settings)
    quote = qualified_quote(role=QuoteRole.TARGET, generation=0)
    current = await store.get()
    await store.replace(current.model_copy(update={"quotes": {quote.instrument: quote}}))
    for code, message in (
        (2105, "HMDS data farm connection is broken:ushmds"),
        (2157, "Sec-def data farm connection is broken:secdefhk"),
    ):
        await store.apply_ibkr_event(
            VenueEvent(venue="IBKR", kind="error", payload={"code": code, "message": message})
        )
    result = (await store.get()).quotes[quote.instrument]
    assert result.subscription_status is SubscriptionStatus.ACTIVE


@pytest.mark.asyncio
async def test_socket_restore_does_not_double_increment_generation(settings: Settings) -> None:
    store = StateStore(settings)
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 1100, "message": "Connectivity between IB and TWS has been lost"},
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 1101, "message": "Connectivity restored - data lost"},
        )
    )
    restored = await store.get()
    assert restored.ibkr.status is ConnectionStatus.CONNECTED
    assert restored.metadata["ibkr_subscription_generation"] == 1
    assert restored.metadata["ibkr_resubscribe_required"] is True
