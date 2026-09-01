from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from zq_arb.adapters.events import VenueEvent
from zq_arb.config import Settings
from zq_arb.domain.enums import AlertSeverity, MarginPreviewStatus
from zq_arb.domain.models import BookLevel, EligibilityStatus, MarketMappingStatus, OrderBook
from zq_arb.services.state import StateStore


@pytest.mark.asyncio
async def test_ibkr_what_if_order_state_becomes_typed_margin_preview(
    settings: Settings,
) -> None:
    store = StateStore(settings)
    context = {
        "order_id": 7001,
        "contract_month": "202609",
        "side": "BUY",
        "quantity": 10,
        "limit_price": "96.330",
    }
    await store.apply_ibkr_event(
        VenueEvent(venue="IBKR", kind="margin_preview_requested", payload=context)
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="margin_preview",
            payload={
                **context,
                "status": "PreSubmitted",
                "init_margin_before": "84,000.00",
                "init_margin_change": "1,250.50",
                "init_margin_after": "85,250.50",
                "maintenance_margin_before": "73,000.00",
                "maintenance_margin_change": "1,100.25",
                "maintenance_margin_after": "74,100.25",
                "equity_with_loan_before": "1,000,000.00",
                "equity_with_loan_change": "0.00",
                "equity_with_loan_after": "1,000,000.00",
                "commission": "20.00",
                "commission_currency": "USD",
                "warning_text": "",
            },
        )
    )
    preview = (await store.get()).margin_preview
    assert preview.status is MarginPreviewStatus.AVAILABLE
    assert preview.next_batch_initial_margin == Decimal("1250.50")
    assert preview.init_margin_after == Decimal("85250.50")
    assert preview.commission == Decimal("20.00")
    assert preview.projected_excess_liquidity == Decimal("914749.50")


@pytest.mark.asyncio
async def test_ibkr_ticks_are_reduced_into_immutable_quote(settings: Settings) -> None:
    store = StateStore(settings)
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 2104, "message": "Market data farm connection is OK:usfuture"},
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(venue="IBKR", kind="market_data_type", payload={"market_data_type": 1})
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="tick_price",
            payload={"month": "202609", "tick_type": 1, "price": "96.30"},
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="tick_price",
            payload={"month": "202609", "tick_type": 2, "price": "96.305"},
        )
    )
    snapshot = await store.get()
    assert snapshot.quotes["202609"].bid == Decimal("96.30")
    assert snapshot.quotes["202609"].ask == Decimal("96.305")
    assert snapshot.quotes["202609"].quality.value == "LIVE"


@pytest.mark.asyncio
async def test_subscriber_is_conflated_to_latest_snapshot(settings: Settings) -> None:
    store = StateStore(settings)
    subscription = store.subscribe()
    first = await anext(subscription)
    await store.set_operating_state(paused=True)
    second = await asyncio.wait_for(anext(subscription), timeout=1)
    assert second.snapshot_id > first.snapshot_id
    assert second.paused
    await subscription.aclose()


@pytest.mark.asyncio
async def test_critical_alert_acknowledgement_stops_flashing(settings: Settings) -> None:
    store = StateStore(settings)
    alert = await store.add_alert(
        AlertSeverity.CRITICAL,
        "UNHEDGED_ZQ",
        "manual action required",
        flashing=True,
    )
    assert await store.acknowledge_alert(alert.alert_id)
    snapshot = await store.get()
    assert snapshot.alerts[0].acknowledged
    assert not snapshot.alerts[0].flashing


@pytest.mark.asyncio
async def test_reference_and_account_events_update_read_model(settings: Settings) -> None:
    store = StateStore(settings)
    await store.set_mapping(
        MarketMappingStatus(verified=True, rule_hash_match=True, market_count_match=True)
    )
    await store.set_eligibility(
        EligibilityStatus(checked=True, blocked=False, country="HK", permitted_for_live=True)
    )
    asset_id = "outcome-asset"
    await store.set_books((OrderBook(token_id=asset_id),))
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="contract_details",
            payload={
                "month": "202609",
                "verified": True,
                "contract_id": 123,
                "local_symbol": "ZQU6",
                "errors": [],
            },
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="account_summary",
            payload={
                "tag": "NetLiquidation",
                "value": "100000",
                "account_fingerprint": "***1234",
            },
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="pnl",
            payload={"daily_pnl": "12", "unrealized_pnl": "5", "realized_pnl": "7"},
        )
    )
    snapshot = await store.get()
    assert snapshot.mapping.verified
    assert snapshot.eligibility.permitted_for_live
    assert asset_id in snapshot.books
    assert snapshot.metadata["contract_verification"]["202609"]["verified"]
    assert snapshot.account.net_liquidation == Decimal("100000")
    assert snapshot.account.daily_pnl == Decimal("12")


@pytest.mark.asyncio
async def test_ibkr_errors_and_configuration_warnings_are_visible(settings: Settings) -> None:
    store = StateStore(settings)
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="configuration_warning",
            payload={"code": "IBKR_ACCOUNT_ID_REQUIRED"},
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 354, "message": "not subscribed"},
        )
    )
    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="error",
            payload={"code": 1100, "message": "connection lost"},
        )
    )
    snapshot = await store.get()
    assert {alert.code for alert in snapshot.alerts} == {
        "IBKR_ACCOUNT_ID_REQUIRED",
        "IBKR_354",
        "IBKR_1100",
    }
    assert snapshot.ibkr.status.value == "DEGRADED"


@pytest.mark.asyncio
async def test_read_only_operating_state_never_arms(settings: Settings) -> None:
    store = StateStore(settings)
    await store.set_operating_state(paused=True, kill_switch=True, armed=True)
    snapshot = await store.get()
    assert snapshot.paused and snapshot.kill_switch
    assert not snapshot.armed
    assert not await store.acknowledge_alert("unknown")


@pytest.mark.asyncio
async def test_manual_reconciliation_is_auditable_state_and_order_events_invalidate_it(
    settings: Settings,
) -> None:
    store = StateStore(settings)
    await store.confirm_reconciliation(
        actor="operator",
        reason="positions checked at both venues",
        snapshot_id=17,
    )
    confirmed = await store.get()
    assert confirmed.reconciliation.clean
    assert confirmed.reconciliation.confirmed_by == "operator"
    assert confirmed.reconciliation.confirmed_snapshot_id == 17

    await store.apply_ibkr_event(
        VenueEvent(
            venue="IBKR",
            kind="order_status",
            payload={"order_id": "42", "status": "Submitted"},
        )
    )
    invalidated = await store.get()
    assert not invalidated.reconciliation.clean
    assert "order status" in invalidated.reconciliation.reason


@pytest.mark.asyncio
async def test_polymarket_stream_events_synchronize_then_disconnect_blocks_books(
    settings: Settings,
) -> None:
    store = StateStore(settings)
    token_id = settings.market_legs[0].yes_token_id
    await store.set_books((OrderBook(token_id=token_id, market="DEC50PLUS_YES"),))
    await store.apply_polymarket_event(
        VenueEvent(venue="POLYMARKET", kind="stream_connected", payload={"token_count": 10})
    )
    await store.apply_polymarket_event(
        VenueEvent(
            venue="POLYMARKET",
            kind="book",
            payload={
                "token_id": token_id,
                "bids": [{"price": "0.10", "size": "100"}],
                "asks": [{"price": "0.12", "size": "100"}],
                "tick_size": "0.01",
            },
        )
    )
    synchronized = await store.get()
    assert synchronized.polymarket.status.value == "CONNECTED"
    assert synchronized.books[token_id].stream_synchronized
    assert synchronized.books[token_id].market == "DEC50PLUS_YES"

    await store.mark_polymarket_books_unsynchronized()
    disconnected = await store.get()
    assert not disconnected.books[token_id].stream_synchronized


@pytest.mark.asyncio
async def test_rest_reconciliation_preserves_match_and_blocks_mismatch(settings: Settings) -> None:
    store = StateStore(settings)
    token_id = settings.market_legs[0].yes_token_id
    live_book = OrderBook(
        token_id=token_id,
        bids=(BookLevel(price=Decimal("0.10"), size=Decimal("100")),),
        asks=(BookLevel(price=Decimal("0.12"), size=Decimal("100")),),
        book_hash="same",
        tick_size=None,
        min_order_size=None,
        source="WEBSOCKET",
        stream_synchronized=True,
    )
    await store.set_books((live_book,))
    matching_rest = live_book.model_copy(
        update={
            "source": "REST",
            "stream_synchronized": False,
            "tick_size": Decimal("0.01"),
            "min_order_size": Decimal("5"),
            "negative_risk": True,
        }
    )
    assert await store.reconcile_polymarket_books((matching_rest,)) == ()
    assert (await store.get()).books[token_id].stream_synchronized
    enriched = (await store.get()).books[token_id]
    assert enriched.tick_size == Decimal("0.01")
    assert enriched.min_order_size == Decimal("5")
    assert enriched.negative_risk is True

    mismatching_rest = matching_rest.model_copy(
        update={
            "asks": (BookLevel(price=Decimal("0.13"), size=Decimal("100")),),
            "book_hash": "different",
        }
    )
    assert await store.reconcile_polymarket_books((mismatching_rest,)) == (token_id,)
    assert not (await store.get()).books[token_id].stream_synchronized
