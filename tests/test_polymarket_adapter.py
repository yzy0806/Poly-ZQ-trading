from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from zq_arb.adapters.events import VenueEvent
from zq_arb.adapters.polymarket import (
    PolymarketAdapter,
    _decimal,
    _json_list,
    _levels,
    _timestamp,
    update_books_from_stream_event,
)
from zq_arb.config import MarketLegConfig, Settings


def market_payload(leg: MarketLegConfig) -> dict[str, object]:
    return {
        "id": leg.market_id,
        "slug": leg.slug,
        "conditionId": leg.condition_id,
        "clobTokenIds": json.dumps([leg.yes_token_id, leg.no_token_id]),
        "outcomes": json.dumps(["Yes", "No"]),
        "orderPriceMinTickSize": str(leg.expected_tick_size),
        "orderMinSize": str(leg.expected_min_order_size),
        "active": True,
        "closed": False,
    }


def test_public_payload_parsers_are_defensive() -> None:
    assert _json_list('["a", "b"]') == ["a", "b"]
    assert _json_list("not-json") == []
    assert _decimal("0.25") == Decimal("0.25")
    assert _decimal("bad") is None
    assert _timestamp("2026-09-16T18:00:00Z") == datetime(2026, 9, 16, 18, tzinfo=UTC)
    assert _timestamp("1750000000000") is not None
    levels = _levels(
        [{"price": "0.2", "size": "4"}, {"price": "0.3", "size": "5"}],
        reverse=True,
    )
    assert [level.price for level in levels] == [Decimal("0.3"), Decimal("0.2")]


@pytest.mark.asyncio
async def test_mapping_and_book_verification_with_mock_transport(settings: Settings) -> None:
    description = "approved test rules"
    configured = settings.model_copy(
        update={"polymarket_event_rule_sha256": hashlib.sha256(description.encode()).hexdigest()}
    )
    event = {
        "id": configured.polymarket_event_id,
        "description": description,
        "markets": [market_payload(leg) for leg in configured.market_legs],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            return httpx.Response(200, json=[event])
        if request.url.path == "/api/geoblock":
            return httpx.Response(200, json={"blocked": False, "country": "HK"})
        if request.url.path == "/book":
            token_id = request.url.params["token_id"]
            return httpx.Response(
                200,
                json={
                    "asset_id": token_id,
                    "bids": [{"price": "0.27", "size": "100"}],
                    "asks": [{"price": "0.28", "size": "200"}],
                    "tick_size": "0.01",
                    "min_order_size": "5",
                    "neg_risk": True,
                    "timestamp": "1750000000000",
                    "hash": "book-hash",
                },
            )
        return httpx.Response(404)

    adapter = PolymarketAdapter(configured)
    await adapter._http.aclose()
    adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mapping = await adapter.verify_market_mapping()
    eligibility = await adapter.check_eligibility()
    book = await adapter.fetch_book(configured.market_legs[0].yes_token_id)
    await adapter.close()
    assert mapping.verified
    assert eligibility.permitted_for_live
    assert book.best_bid == Decimal("0.27")
    assert book.best_ask == Decimal("0.28")
    assert book.negative_risk
    assert book.source == "REST"
    assert not book.stream_synchronized


def test_leg_mismatch_is_explicit(settings: Settings) -> None:
    leg = settings.market_legs[0]
    payload = market_payload(leg)
    payload["conditionId"] = "wrong"
    errors: list[str] = []
    PolymarketAdapter._verify_leg(leg, payload, errors)
    assert f"{leg.code}: condition id mismatch" in errors


def test_market_websocket_snapshot_and_delta_update_the_authoritative_book() -> None:
    snapshot_event = VenueEvent(
        venue="POLYMARKET",
        kind="book",
        payload={
            "token_id": "token-1",
            "market": "condition-1",
            "bids": [{"price": "0.27", "size": "100"}],
            "asks": [{"price": "0.29", "size": "200"}],
            "tick_size": "0.01",
            "min_order_size": "5",
            "hash": "hash-1",
            "timestamp": "2026-08-26T04:00:00Z",
        },
    )
    (book,) = update_books_from_stream_event({}, snapshot_event)
    assert book.stream_synchronized
    assert book.source == "WEBSOCKET"
    assert book.best_bid == Decimal("0.27")

    delta_event = VenueEvent(
        venue="POLYMARKET",
        kind="price_change",
        payload={
            "price_changes": [
                {
                    "token_id": "token-1",
                    "price": "0.28",
                    "size": "75",
                    "side": "BUY",
                    "hash": "hash-2",
                },
                {
                    "token_id": "token-1",
                    "price": "0.27",
                    "size": "0",
                    "side": "BUY",
                    "hash": "hash-2",
                },
            ],
            "timestamp": "2026-08-26T04:00:01Z",
        },
    )
    (updated,) = update_books_from_stream_event({book.token_id: book}, delta_event)
    assert updated.best_bid == Decimal("0.28")
    assert [level.price for level in updated.bids] == [Decimal("0.28")]
    assert updated.book_hash == "hash-2"


def test_market_websocket_book_preserves_rest_static_metadata_when_omitted() -> None:
    seeded = update_books_from_stream_event(
        {},
        VenueEvent(
            venue="POLYMARKET",
            kind="book",
            payload={
                "token_id": "token-1",
                "bids": [{"price": "0.520", "size": "100"}],
                "asks": [{"price": "0.530", "size": "100"}],
                "tick_size": "0.001",
                "min_order_size": "5",
                "neg_risk": True,
            },
        ),
    )[0]
    (updated,) = update_books_from_stream_event(
        {seeded.token_id: seeded},
        VenueEvent(
            venue="POLYMARKET",
            kind="book",
            payload={
                "token_id": "token-1",
                "bids": [{"price": "0.521", "size": "110"}],
                "asks": [{"price": "0.531", "size": "90"}],
            },
        ),
    )
    assert updated.tick_size == Decimal("0.001")
    assert updated.min_order_size == Decimal("5")
    assert updated.negative_risk is True


def test_market_websocket_delta_requires_a_seed_book() -> None:
    event = VenueEvent(
        venue="POLYMARKET",
        kind="price_change",
        payload={
            "price_changes": [{"token_id": "missing", "price": "0.20", "size": "1", "side": "BUY"}]
        },
    )
    with pytest.raises(RuntimeError, match="before a complete book snapshot"):
        update_books_from_stream_event({}, event)
