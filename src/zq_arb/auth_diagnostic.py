"""Isolated wallet/authentication checks. Never constructs a trading client."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from eth_account import Account
from eth_utils.address import to_checksum_address
from polymarket._internal.actions.auth import build_l1_auth_headers
from polymarket._internal.environment import PRODUCTION_CONFIG
from polymarket._internal.hmac import build_hmac_signature
from polymarket._internal.l1_auth import sign_api_key_auth
from polymarket._internal.wallet import classify_wallet_type, signature_type_for
from polymarket.models import ApiKeyCreds
from pydantic import SecretStr

from zq_arb.config import Settings

CLOB = "https://clob.polymarket.com"
RELAYER = "https://relayer-v2.polymarket.com"
DATA = "https://data-api.polymarket.com"
ALLOWED = frozenset(
    {
        ("GET", RELAYER + "/deployed"),
        ("POST", CLOB + "/auth/api-key"),
        ("GET", CLOB + "/auth/derive-api-key"),
        ("GET", CLOB + "/auth/api-keys"),
        ("GET", CLOB + "/data/orders"),
        ("GET", CLOB + "/balance-allowance"),
        ("GET", CLOB + "/data/trades"),
        ("GET", DATA + "/activity"),
    }
)


class DiagnosticFailure(Exception):
    """A diagnostic failure with a safe, locally authored explanation."""


async def check_auth(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    include_history: bool = False,
) -> dict[str, Any]:
    """Return redacted venue response bodies, including failed bootstrap attempts.

    The sole permitted POST creates CLOB credentials, as in the installed SDK.
    Redirects are disabled; no order, approval or wallet-deployment endpoint is allowed.
    """
    secrets = {
        value.get_secret_value()
        for name in type(settings).model_fields
        if isinstance(value := getattr(settings, name), SecretStr)
        and "address" not in name
        and value.get_secret_value()
    }
    report: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "test_2": {"status": "FAIL"},
        "test_3": {"status": "SKIPPED"},
        "responses": [],
        "scope": "No orders, transfers, approvals or deployment; open orders first page only.",
    }
    if include_history:
        report["trade_history"] = {"status": "SKIPPED"}
        report["wallet_activity"] = {"status": "SKIPPED"}
        report["history_scope"] = "First trade page and latest 20 Polymarket activity records only."

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "[REDACTED]"
                if any(
                    marker in str(key).lower().replace("_", "").replace("-", "")
                    for marker in ("apikey", "secret", "passphrase", "privatekey", "signature")
                )
                or key == "key"
                else redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            return value
        return value

    async with httpx.AsyncClient(timeout=20, follow_redirects=False, transport=transport) as client:

        async def request(
            method: str,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            params: dict[str, str | int] | None = None,
        ) -> tuple[int, Any]:
            if (method, url) not in ALLOWED:
                raise DiagnosticFailure("Endpoint is outside the diagnostic allowlist.")
            if headers:
                secrets.update(
                    v
                    for k, v in headers.items()
                    if k in {"POLY_SIGNATURE", "POLY_API_KEY", "POLY_PASSPHRASE"}
                )
            entry: dict[str, Any] = {"method": method, "url": url}
            report["responses"].append(entry)
            try:
                response = await client.request(method, url, headers=headers, params=params)
            except httpx.HTTPError as error:
                entry.update(status=None, local_error=type(error).__name__)
                raise DiagnosticFailure("No HTTP response received; see local_error.") from None
            entry["status"] = response.status_code
            try:
                body = response.json()
            except ValueError:
                # An unstructured credential response could expose unknown new secrets.
                body = {"non_json_response": "Body withheld because it cannot be safely redacted."}
            entry["body"] = body
            return response.status_code, body

        stage = "test_2"
        try:
            try:
                signer = Account.from_key(settings.polymarket_private_key.get_secret_value())
            except Exception:
                raise DiagnosticFailure("POLYMARKET_PRIVATE_KEY is missing or invalid.") from None
            report[stage]["derived_signer"] = signer.address
            try:
                expected = to_checksum_address(
                    settings.polymarket_signer_address.get_secret_value()
                )
                wallet = to_checksum_address(settings.polymarket_funder_address.get_secret_value())
            except ValueError:
                raise DiagnosticFailure("Configured signer or funder address is invalid.") from None
            report[stage].update(configured_signer=expected, wallet=wallet)
            if expected != signer.address:
                raise DiagnosticFailure("Private key does not match POLYMARKET_SIGNER_ADDRESS.")
            try:
                kind = classify_wallet_type(
                    signer=signer.address, wallet=wallet, config=PRODUCTION_CONFIG.wallet_derivation
                )
            except Exception:
                raise DiagnosticFailure(
                    "Funder does not match this signer or its supported deterministic wallets."
                ) from None
            report[stage]["wallet_type"] = kind
            if kind != "EOA":
                status, body = await request(
                    "GET",
                    RELAYER + "/deployed",
                    params={
                        "address": wallet,
                        "type": {
                            "DEPOSIT_WALLET": "WALLET",
                            "POLY_PROXY": "PROXY",
                            "GNOSIS_SAFE": "SAFE",
                        }[kind],
                    },
                )
                if status != 200 or not isinstance(body, dict) or body.get("deployed") is not True:
                    raise DiagnosticFailure("Venue did not confirm this wallet is deployed.")
            report[stage]["status"] = "PASS"
            stage = "test_3"
            report[stage]["status"] = "FAIL"
            if settings.polymarket_clob_host.rstrip("/") != CLOB:
                raise DiagnosticFailure("Diagnostic only authenticates to the official CLOB host.")
            parts = [
                settings.polymarket_api_key.get_secret_value(),
                settings.polymarket_api_secret.get_secret_value(),
                settings.polymarket_api_passphrase.get_secret_value(),
            ]
            if any(parts) and not settings.clob_credentials_configured:
                raise DiagnosticFailure(
                    "CLOB credential override is incomplete or contains placeholders."
                )
            if settings.polymarket_credential_nonce < 0:
                raise DiagnosticFailure("Credential nonce must be nonnegative.")
            if settings.clob_credentials_configured:
                credentials = ApiKeyCreds(key=parts[0], secret=parts[1], passphrase=parts[2])
                report[stage]["credential_source"] = "configured override"
            else:
                report[stage]["credential_source"] = "SDK create-or-derive flow"
                headers = build_l1_auth_headers(
                    sign_api_key_auth(
                        signer,
                        chain_id=PRODUCTION_CONFIG.chain_id,
                        timestamp=int(time.time()),
                        nonce=settings.polymarket_credential_nonce,
                    )
                )
                status, body = await request("POST", CLOB + "/auth/api-key", headers=headers)
                if status == 400:
                    status, body = await request(
                        "GET", CLOB + "/auth/derive-api-key", headers=headers
                    )
                if status != 200:
                    raise DiagnosticFailure(
                        "CLOB credential bootstrap failed; see venue responses."
                    )
                credentials = ApiKeyCreds.parse_response(body)
            secrets.update((credentials.key, credentials.secret, credentials.passphrase))
            reads: tuple[tuple[str, dict[str, str | int] | None], ...] = (
                ("/auth/api-keys", None),
                ("/data/orders", None),
                (
                    "/balance-allowance",
                    {"asset_type": "COLLATERAL", "signature_type": signature_type_for(kind)},
                ),
            )
            if include_history:
                reads += (("/data/trades", None),)
            for path, params in reads:
                if path == "/data/trades":
                    report[stage]["status"] = "PASS"
                    stage = "trade_history"
                    report[stage]["status"] = "FAIL"
                timestamp = int(time.time())
                headers = {
                    "POLY_ADDRESS": signer.address,
                    "POLY_API_KEY": credentials.key,
                    "POLY_PASSPHRASE": credentials.passphrase,
                    "POLY_TIMESTAMP": str(timestamp),
                    "POLY_SIGNATURE": build_hmac_signature(
                        secret=credentials.secret, timestamp=timestamp, method="GET", path=path
                    ),
                }
                status, body = await request("GET", CLOB + path, headers=headers, params=params)
                if status != 200:
                    raise DiagnosticFailure(
                        "Authenticated CLOB request failed; see venue responses."
                    )
                if not isinstance(body, dict):
                    raise DiagnosticFailure("CLOB response has an unexpected shape.")
                if path == "/auth/api-keys" and credentials.key not in body.get("apiKeys", []):
                    raise DiagnosticFailure(
                        "Configured/derived API key is not in the active key list."
                    )
                if path == "/data/orders" and not isinstance(body.get("data"), list):
                    raise DiagnosticFailure("Open orders response has an unexpected shape.")
                if path == "/balance-allowance" and "balance" not in body:
                    raise DiagnosticFailure("Balance response has an unexpected shape.")
                if path == "/data/trades":
                    if not isinstance(body.get("data"), list):
                        raise DiagnosticFailure("Trade history response has an unexpected shape.")
                    report[stage]["records_returned"] = len(body["data"])
            report[stage]["status"] = "PASS"
            if include_history:
                stage = "wallet_activity"
                report[stage]["status"] = "FAIL"
                status, body = await request(
                    "GET",
                    DATA + "/activity",
                    params={
                        "user": wallet,
                        "limit": 20,
                        "offset": 0,
                        "sortBy": "TIMESTAMP",
                        "sortDirection": "DESC",
                        "excludeDepositsWithdrawals": "false",
                    },
                )
                if status != 200 or not isinstance(body, list):
                    raise DiagnosticFailure("Wallet activity request failed; see venue response.")
                report[stage].update(status="PASS", records_returned=len(body))
        except DiagnosticFailure as error:
            report[stage]["local_error"] = str(error)
        except Exception as error:
            # SDK exception strings may contain signing material. Preserve only the type.
            report[stage]["local_error"] = "Local SDK/configuration error: " + type(error).__name__
    return cast(dict[str, Any], redact(report))
