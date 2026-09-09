from __future__ import annotations

import asyncio
import traceback
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

from zq_arb.adapters.polymarket import PolymarketAdapter, PolymarketProtocolError
from zq_arb.config import Settings


def signing_settings(settings: Settings, **updates: object) -> Settings:
    return settings.model_copy(
        update={
            "polymarket_private_key": SecretStr("dummy-signing-material"),
            "polymarket_funder_address": SecretStr("0x0000000000000000000000000000000000000001"),
            "polymarket_api_key": SecretStr(""),
            "polymarket_api_secret": SecretStr(""),
            "polymarket_api_passphrase": SecretStr(""),
            **updates,
        }
    )


@pytest.mark.asyncio
async def test_automatic_credentials_bootstrap_once_and_cache(settings: Settings) -> None:
    configured = signing_settings(settings, polymarket_credential_nonce=7)
    adapter = PolymarketAdapter(configured)
    client = AsyncMock()
    try:
        with patch(
            "polymarket.AsyncSecureClient.create", new_callable=AsyncMock, return_value=client
        ) as create:
            clients = await asyncio.gather(*(adapter._authenticated_client() for _ in range(8)))
            assert all(item is client for item in clients)
            create.assert_awaited_once_with(
                private_key="dummy-signing-material",
                wallet="0x0000000000000000000000000000000000000001",
                nonce=7,
            )
        assert not configured.clob_credentials_configured
        assert not any("CLOB" in error for error in configured.live_readiness_errors())
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_explicit_credentials_override_automatic_setup(settings: Settings) -> None:
    configured = signing_settings(
        settings,
        polymarket_credential_nonce=7,
        polymarket_api_key=SecretStr("dummy-key"),
        polymarket_api_secret=SecretStr("dummy-secret"),
        polymarket_api_passphrase=SecretStr("dummy-passphrase"),
    )
    adapter = PolymarketAdapter(configured)
    try:
        with patch(
            "polymarket.AsyncSecureClient.create", new_callable=AsyncMock, return_value=AsyncMock()
        ) as create:
            await adapter._authenticated_client()
            options = create.call_args.kwargs
            assert "nonce" not in options
            assert options["credentials"].key == "dummy-key"
            assert (
                options["credentials"].secret == configured.polymarket_api_secret.get_secret_value()
            )
            assert options["credentials"].passphrase == (
                configured.polymarket_api_passphrase.get_secret_value()
            )
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_relayer_credentials_are_forwarded_only_when_enabled(
    settings: Settings,
    enabled: bool,
) -> None:
    adapter = PolymarketAdapter(
        signing_settings(
            settings,
            polymarket_relayer_enabled=enabled,
            polymarket_relayer_api_key=SecretStr("dummy-relayer-key"),
            polymarket_relayer_api_key_address=SecretStr(
                "0x0000000000000000000000000000000000000001"
            ),
        )
    )
    try:
        with patch(
            "polymarket.AsyncSecureClient.create", new_callable=AsyncMock, return_value=AsyncMock()
        ) as create:
            await adapter._authenticated_client()
            options = create.call_args.kwargs
            if enabled:
                from polymarket.auth import RelayerApiKey

                assert isinstance(options["api_key"], RelayerApiKey)
                assert options["api_key"].key == "dummy-relayer-key"
            else:
                assert "api_key" not in options
            assert "credentials" not in options
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"polymarket_private_key": SecretStr("")}, "signing key"),
        ({"polymarket_funder_address": SecretStr("")}, "funder wallet"),
        ({"polymarket_api_key": SecretStr("dummy-key")}, "all three CLOB"),
        ({"polymarket_relayer_enabled": True}, "enabled relayer"),
        ({"polymarket_credential_nonce": -1}, "nonnegative"),
    ],
)
async def test_incomplete_auth_fails_before_sdk_calls(
    settings: Settings, updates, reason: str
) -> None:
    adapter = PolymarketAdapter(signing_settings(settings, **updates))
    try:
        with patch("polymarket.AsyncSecureClient.create", new_callable=AsyncMock) as create:
            with pytest.raises(PermissionError, match=reason):
                await adapter._authenticated_client()
            create.assert_not_awaited()
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_bootstrap_failure_is_redacted_and_can_retry(settings: Settings) -> None:
    adapter = PolymarketAdapter(signing_settings(settings))
    try:
        with patch(
            "polymarket.AsyncSecureClient.create",
            new_callable=AsyncMock,
            side_effect=[ValueError("dummy-signing-material"), AsyncMock()],
        ) as create:
            with pytest.raises(PolymarketProtocolError) as failure:
                await adapter._authenticated_client()
            rendered = "".join(traceback.format_exception(failure.value))
            assert "dummy-signing-material" not in rendered
            assert adapter._secure_client is None
            await adapter._authenticated_client()
            assert create.await_count == 2
    finally:
        await adapter.close()
