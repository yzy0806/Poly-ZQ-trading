from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from zq_arb.api.security import SessionManager
from zq_arb.config import Settings, validate_environment_schema
from zq_arb.observability import redact_value


def settings_payload(settings: Settings) -> dict[str, object]:
    return settings.model_dump(exclude={"cors_origins", "reference_contract_months", "market_legs"})


def test_child_quantity_safety_invariant(settings: Settings) -> None:
    payload = settings_payload(settings)
    payload["ibkr_zq_child_order_quantity"] = 9
    with pytest.raises(ValidationError, match="exactly 10"):
        Settings.model_validate(payload)


def test_live_mode_rejects_zero_reserves(settings: Settings) -> None:
    payload = settings_payload(settings)
    payload.update(
        {
            "run_mode": "LIVE_ARMED",
            "live_trading_enabled": True,
            "ibkr_order_submission_enabled": True,
            "polymarket_order_submission_enabled": True,
            "operator_approval_id": "approval",
            "polymarket_api_key": SecretStr("configured"),
            "polymarket_api_secret": SecretStr("configured"),
            "polymarket_api_passphrase": SecretStr("configured"),
        }
    )
    with pytest.raises(ValidationError, match="must be positive"):
        Settings.model_validate(payload)


def test_unknown_and_duplicate_env_keys_are_rejected(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    actual = tmp_path / ".env"
    example.write_text("KNOWN=value\n", encoding="utf-8")
    actual.write_text("KNOWN=one\nKNOWN=two\nUNKNOWN=three\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"duplicate variables.*unknown variables"):
        validate_environment_schema(actual, example)


def test_recursive_secret_redaction() -> None:
    value = {"nested": {"api_key": "never-log", "safe": "visible"}, "items": [{"password": "x"}]}
    assert redact_value(value) == {
        "nested": {"api_key": "[REDACTED]", "safe": "visible"},
        "items": [{"password": "[REDACTED]"}],
    }


def test_session_tokens_are_signed_and_expiring(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "dashboard_username": "operator",
            "dashboard_password": SecretStr("test-password"),
            "session_signing_key": SecretStr("a-long-test-signing-key"),
        }
    )
    manager = SessionManager(configured)
    token, identity = manager.create("operator")
    decoded = manager.decode(token)
    assert decoded == identity
    assert manager.decode(token + "tampered") is None
    assert manager.authenticate_password("operator", "test-password")
    assert not manager.authenticate_password("operator", "wrong")
