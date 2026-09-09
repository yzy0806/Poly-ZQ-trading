from __future__ import annotations

import json

import httpx
import pytest
from eth_account import Account
from polymarket._internal.environment import PRODUCTION_CONFIG
from polymarket._internal.wallet import derive_safe_wallet_address
from pydantic import SecretStr

from zq_arb.auth_diagnostic import ALLOWED, check_auth
from zq_arb.config import Settings


def configured(settings: Settings) -> Settings:
    key = "11" * 32
    signer = Account.from_key(key).address
    return settings.model_copy(
        update={
            "polymarket_private_key": SecretStr(key),
            "polymarket_signer_address": SecretStr(signer),
            "polymarket_funder_address": SecretStr(
                derive_safe_wallet_address(signer, PRODUCTION_CONFIG.wallet_derivation)
            ),
            "polymarket_api_key": SecretStr(""),
            "polymarket_api_secret": SecretStr(""),
            "polymarket_api_passphrase": SecretStr(""),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "undeployed", "unauthorized", "bad_shape"])
async def test_venue_responses_and_no_transaction_requests(
    settings: Settings, failure: str
) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert (request.method, str(request.url).split("?")[0]) in ALLOWED
        assert "RELAYER_API_KEY" not in request.headers
        path = request.url.path
        if path == "/deployed":
            return httpx.Response(200, json={"deployed": failure != "undeployed"})
        if path == "/auth/api-key":
            return httpx.Response(400, json={"error": "Could not create api key"})
        if path == "/auth/derive-api-key":
            return httpx.Response(
                200,
                json={
                    "apiKey": "test-api-key",
                    "secret": "c2VjcmV0",
                    "passphrase": "test-passphrase",
                },
            )
        assert request.headers["POLY_API_KEY"] == "test-api-key"
        assert request.headers["POLY_SIGNATURE"]
        if failure == "unauthorized":
            return httpx.Response(401, json={"error": "Invalid api key: test-api-key"})
        if path == "/auth/api-keys":
            return httpx.Response(200, json={"apiKeys": ["test-api-key"]})
        if path == "/data/orders":
            return httpx.Response(
                200,
                json={}
                if failure == "bad_shape"
                else {"data": [], "next_cursor": "LTE=", "count": 0},
            )
        return httpx.Response(200, json={"balance": "0", "allowances": {}})

    report = await check_auth(configured(settings), transport=httpx.MockTransport(handle))
    rendered = json.dumps(report)
    for secret in ("11" * 32, "test-api-key", "c2VjcmV0", "test-passphrase"):
        assert secret not in rendered
    if failure == "undeployed":
        assert len(requests) == 1
        assert report["test_2"]["status"] == "FAIL"
        assert report["test_3"]["status"] == "SKIPPED"
    else:
        assert report["test_2"]["status"] == "PASS"
        assert report["responses"][1]["body"] == {"error": "Could not create api key"}
        assert report["test_3"]["status"] == ("PASS" if failure is None else "FAIL")
        if failure == "unauthorized":
            assert report["responses"][-1]["status"] == 401
            assert report["responses"][-1]["body"] == {"error": "Invalid api key: [REDACTED]"}


@pytest.mark.asyncio
async def test_wrong_signer_stops_before_network(settings: Settings) -> None:
    settings = configured(settings).model_copy(
        update={"polymarket_signer_address": SecretStr("0x" + "22" * 20)}
    )

    def reject(request: httpx.Request) -> httpx.Response:
        pytest.fail("No network access should occur on a signer mismatch")

    report = await check_auth(settings, transport=httpx.MockTransport(reject))
    assert report["test_2"]["status"] == "FAIL"
    assert "does not match" in report["test_2"]["local_error"]
    assert report["responses"] == []


@pytest.mark.asyncio
async def test_network_failure_has_no_invented_venue_response(settings: Settings) -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Sensitive exception detail must not be printed")

    report = await check_auth(configured(settings), transport=httpx.MockTransport(reject))
    assert report["responses"][0]["status"] is None
    assert report["responses"][0]["local_error"] == "ConnectError"
    assert "Sensitive" not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_history", [False, True])
async def test_explicit_credentials_never_bootstrap(
    settings: Settings, include_history: bool
) -> None:
    settings = configured(settings).model_copy(
        update={
            "polymarket_api_key": SecretStr("existing-key"),
            "polymarket_api_secret": SecretStr("c2VjcmV0"),
            "polymarket_api_passphrase": SecretStr("existing-passphrase"),
        }
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/deployed":
            return httpx.Response(200, json={"deployed": True})
        if request.url.path == "/activity":
            assert "POLY_API_KEY" not in request.headers
            assert (
                request.url.params["user"] == settings.polymarket_funder_address.get_secret_value()
            )
            return httpx.Response(200, json=[{"type": "TRADE", "transactionHash": "0x123"}])
        assert request.headers["POLY_API_KEY"] == "existing-key"
        if request.url.path == "/auth/api-keys":
            return httpx.Response(200, json={"apiKeys": ["existing-key"]})
        if request.url.path == "/data/orders":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/data/trades":
            return httpx.Response(200, json={"data": [{"id": "historical-trade"}]})
        return httpx.Response(200, json={"balance": "0"})

    report = await check_auth(
        settings, transport=httpx.MockTransport(handle), include_history=include_history
    )
    assert report["test_3"]["status"] == "PASS"
    assert report["test_3"]["credential_source"] == "configured override"
    assert "existing-key" not in json.dumps(report)
    if include_history:
        assert report["trade_history"] == {"status": "PASS", "records_returned": 1}
        assert report["wallet_activity"] == {"status": "PASS", "records_returned": 1}
