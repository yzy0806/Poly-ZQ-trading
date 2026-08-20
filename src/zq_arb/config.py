from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from zq_arb.domain.enums import RunMode

PLACEHOLDER_MARKERS = ("REPLACE_", "REQUIRED_", "<your-", "<")


class MarketLegConfig(BaseSettings):
    code: str
    label: str
    market_id: str
    slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    expected_tick_size: Decimal
    expected_min_order_size: Decimal

    model_config = SettingsConfigDict(extra="forbid", frozen=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    env_file_version: int = 2
    app_env: str = "development"
    run_mode: RunMode = RunMode.READ_ONLY
    live_trading_enabled: bool = False
    polymarket_order_submission_enabled: bool = False
    ibkr_order_submission_enabled: bool = False
    operator_approval_id: str = ""
    config_version: str
    strategy_version: str

    api_host: str
    api_port: int
    api_workers: int = 1
    dashboard_origin: str
    dashboard_username: str
    dashboard_password: SecretStr
    session_signing_key: SecretStr
    control_confirmation_secret: SecretStr
    cookie_secure: bool = False
    cors_allowed_origins: str
    dashboard_session_max_age_seconds: int = 28_800

    runtime_data_dir: Path
    database_url: str
    log_dir: Path
    audit_export_dir: Path
    sqlite_busy_timeout_ms: int = 5_000
    sqlite_wal_autocheckpoint_pages: int = 1_000
    retention_days_quotes: int = 30
    retention_days_audit: int = 2_555

    ibkr_host: str
    ibkr_port: int
    ibkr_python_api_path: Path
    ibkr_client_id: int
    ibkr_account_id: SecretStr
    ibkr_trading_mode: str
    ibkr_market_data_type: int
    ibkr_connect_timeout_seconds: int
    ibkr_heartbeat_seconds: int
    ibkr_reconnect_max_attempts: int
    ibkr_reconnect_backoff_seconds: int
    ibkr_zq_exchange: str
    ibkr_zq_currency: str
    ibkr_zq_trading_class: str
    ibkr_zq_contract_month: str
    ibkr_zq_reference_contract_months: str
    ibkr_zq_child_order_quantity: int
    ibkr_zq_order_type: str
    ibkr_zq_time_in_force: str
    ibkr_zq_auto_reprice_enabled: bool
    ibkr_zq_max_price_revisions: int
    ibkr_zq_price_revision_seconds: int

    polymarket_clob_host: str
    polymarket_data_api_host: str
    polymarket_gamma_api_host: str
    polymarket_relayer_host: str
    polymarket_market_ws_url: str
    polymarket_user_ws_url: str
    polymarket_geoblock_url: str
    polymarket_chain_id: int
    polygon_rpc_url: str = ""
    polymarket_private_key: SecretStr
    polymarket_signer_address: SecretStr
    polymarket_funder_address: SecretStr
    polymarket_signature_type: str
    polymarket_credential_nonce: int
    polymarket_api_key: SecretStr
    polymarket_api_secret: SecretStr
    polymarket_api_passphrase: SecretStr
    polymarket_event_id: str
    polymarket_event_slug: str
    polymarket_event_title: str
    polymarket_event_start_utc: datetime
    polymarket_event_end_utc: datetime
    polymarket_event_market_count: int
    polymarket_event_neg_risk: bool
    polymarket_event_rule_sha256: str
    polymarket_event_rule_hash_canonicalization: str
    polymarket_resolution_calendar_url: str
    polymarket_resolution_statement_url: str
    polymarket_default_order_type: str
    polymarket_post_only: bool
    polymarket_post_only_reprice_seconds: int
    polymarket_post_only_max_reprices: int
    polymarket_hard_price_cap: Decimal
    polymarket_cancel_confirm_timeout_seconds: int
    polymarket_user_ws_ping_seconds: int
    polymarket_reconnect_max_attempts: int
    polymarket_reconnect_backoff_seconds: int
    polymarket_book_snapshot_interval_seconds: int = 30
    polymarket_emergency_order_type: str
    polymarket_emergency_max_price: Decimal
    polymarket_hedge_rounding_mode: str

    polymarket_dec50plus_market_id: str
    polymarket_dec50plus_market_slug: str
    polymarket_dec50plus_condition_id: str
    polymarket_dec50plus_yes_token_id: str
    polymarket_dec50plus_no_token_id: str
    polymarket_dec50plus_expected_tick_size: Decimal
    polymarket_dec50plus_expected_min_order_size: Decimal
    polymarket_dec25_market_id: str
    polymarket_dec25_market_slug: str
    polymarket_dec25_condition_id: str
    polymarket_dec25_yes_token_id: str
    polymarket_dec25_no_token_id: str
    polymarket_dec25_expected_tick_size: Decimal
    polymarket_dec25_expected_min_order_size: Decimal
    polymarket_no_change_market_id: str
    polymarket_no_change_market_slug: str
    polymarket_no_change_condition_id: str
    polymarket_no_change_yes_token_id: str
    polymarket_no_change_no_token_id: str
    polymarket_no_change_expected_tick_size: Decimal
    polymarket_no_change_expected_min_order_size: Decimal
    polymarket_inc25_market_id: str
    polymarket_inc25_market_slug: str
    polymarket_inc25_condition_id: str
    polymarket_inc25_yes_token_id: str
    polymarket_inc25_no_token_id: str
    polymarket_inc25_expected_tick_size: Decimal
    polymarket_inc25_expected_min_order_size: Decimal
    polymarket_inc50plus_market_id: str
    polymarket_inc50plus_market_slug: str
    polymarket_inc50plus_condition_id: str
    polymarket_inc50plus_yes_token_id: str
    polymarket_inc50plus_no_token_id: str
    polymarket_inc50plus_expected_tick_size: Decimal
    polymarket_inc50plus_expected_min_order_size: Decimal

    min_net_profit_usd: Decimal
    min_return_on_capital_bps: Decimal
    max_zq_position: int
    max_open_batches: int
    max_unhedged_zq_contracts: int
    max_unhedged_seconds: int
    max_zq_slippage_ticks: int
    max_polymarket_price_slippage: Decimal
    min_full_excess_liquidity_usd: Decimal
    min_excess_liquidity_margin_multiplier: Decimal
    min_margin_cushion_ratio: Decimal
    max_daily_loss_usd: Decimal
    max_strategy_drawdown_usd: Decimal
    tail_loss_gate_enabled: bool
    max_tail_loss_usd: Decimal | None = None
    max_quote_age_ms: int
    max_cross_venue_timestamp_skew_ms: int
    max_clock_drift_ms: int
    model_risk_reserve_usd: Decimal
    operational_risk_reserve_usd: Decimal
    effr_basis_reserve_usd: Decimal

    internal_timezone: str
    operator_timezone: str
    fomc_timezone: str
    cme_timezone: str
    fomc_trading_cutoff_utc: datetime
    fomc_statement_utc: datetime
    fomc_post_decision_resume_enabled: bool
    fomc_trading_resume_utc: datetime | None = None
    geoblock_refresh_seconds: int

    log_level: str
    log_format: str
    log_redact_secrets: bool
    prometheus_enabled: bool
    prometheus_host: str
    prometheus_port: int
    alert_webhook_url: str = ""
    alert_email_to: str = ""
    software_version: str
    engine_state_publish_interval_ms: int = 500
    event_queue_maxsize: int = 10_000
    simulate_polymarket_fills: bool
    use_recorded_market_data: bool
    recorded_market_data_path: Path | None = None
    deterministic_random_seed: int
    fail_on_unknown_env_var: bool

    @field_validator("ibkr_zq_contract_month")
    @classmethod
    def validate_contract_month(cls, value: str) -> str:
        if not re.fullmatch(r"\d{6}", value):
            raise ValueError("IBKR_ZQ_CONTRACT_MONTH must use YYYYMM")
        return value

    @field_validator(
        "max_tail_loss_usd",
        "fomc_trading_resume_utc",
        "recorded_market_data_path",
        mode="before",
    )
    @classmethod
    def blank_optional_value(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def enforce_safety_invariants(self) -> Self:
        errors: list[str] = []
        if self.env_file_version != 2:
            errors.append("ENV_FILE_VERSION must be 2")
        if self.api_workers != 1:
            errors.append("API_WORKERS must be 1 for deterministic state ownership")
        if self.ibkr_zq_child_order_quantity != 10:
            errors.append("IBKR_ZQ_CHILD_ORDER_QUANTITY must be exactly 10")
        if self.max_open_batches != 1:
            errors.append("MAX_OPEN_BATCHES must be exactly 1")
        if self.max_zq_position > 100:
            errors.append("MAX_ZQ_POSITION cannot exceed 100")
        if self.ibkr_zq_order_type.upper() != "LMT":
            errors.append("IBKR_ZQ_ORDER_TYPE must be LMT")
        if self.ibkr_zq_time_in_force.upper() != "DAY":
            errors.append("IBKR_ZQ_TIME_IN_FORCE must be DAY")
        if self.ibkr_zq_auto_reprice_enabled or self.ibkr_zq_max_price_revisions != 0:
            errors.append("automatic ZQ repricing must remain disabled")
        if self.polymarket_default_order_type.upper() != "GTC" or not self.polymarket_post_only:
            errors.append("the default Polymarket path must be post-only GTC")
        if self.fomc_post_decision_resume_enabled:
            errors.append("post-decision resumption is prohibited for version 1")
        if self.fomc_trading_cutoff_utc >= self.fomc_statement_utc:
            errors.append("FOMC cutoff must precede the statement")
        if self.run_mode.is_live:
            errors.extend(self.live_readiness_errors())
        if errors:
            raise ValueError("; ".join(dict.fromkeys(errors)))
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins(self) -> tuple[str, ...]:
        return tuple(
            value.strip() for value in self.cors_allowed_origins.split(",") if value.strip()
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reference_contract_months(self) -> tuple[str, ...]:
        months = tuple(
            value.strip() for value in self.ibkr_zq_reference_contract_months.split(",") if value
        )
        if len(months) != 4 or any(not re.fullmatch(r"\d{6}", month) for month in months):
            raise ValueError("IBKR_ZQ_REFERENCE_CONTRACT_MONTHS must contain four YYYYMM values")
        return months

    @computed_field  # type: ignore[prop-decorator]
    @property
    def market_legs(self) -> tuple[MarketLegConfig, ...]:
        return (
            self._market_leg("DEC50PLUS", "Decrease 50+"),
            self._market_leg("DEC25", "Decrease 25"),
            self._market_leg("NO_CHANGE", "No change"),
            self._market_leg("INC25", "Increase 25"),
            self._market_leg("INC50PLUS", "Increase 50+"),
        )

    def _market_leg(self, code: str, label: str) -> MarketLegConfig:
        prefix = f"polymarket_{code.lower()}"
        return MarketLegConfig(
            code=code,
            label=label,
            market_id=getattr(self, f"{prefix}_market_id"),
            slug=getattr(self, f"{prefix}_market_slug"),
            condition_id=getattr(self, f"{prefix}_condition_id"),
            yes_token_id=getattr(self, f"{prefix}_yes_token_id"),
            no_token_id=getattr(self, f"{prefix}_no_token_id"),
            expected_tick_size=getattr(self, f"{prefix}_expected_tick_size"),
            expected_min_order_size=getattr(self, f"{prefix}_expected_min_order_size"),
        )

    @staticmethod
    def _is_configured(secret: SecretStr | str) -> bool:
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        return bool(value and not any(marker in value for marker in PLACEHOLDER_MARKERS))

    @property
    def dashboard_auth_configured(self) -> bool:
        return all(
            self._is_configured(value)
            for value in (
                self.dashboard_username,
                self.dashboard_password,
                self.session_signing_key,
                self.control_confirmation_secret,
            )
        )

    @property
    def ibkr_account_configured(self) -> bool:
        return self._is_configured(self.ibkr_account_id)

    @property
    def clob_credentials_configured(self) -> bool:
        return all(
            self._is_configured(value)
            for value in (
                self.polymarket_api_key,
                self.polymarket_api_secret,
                self.polymarket_api_passphrase,
            )
        )

    def live_readiness_errors(self) -> list[str]:
        errors: list[str] = []
        if not self.live_trading_enabled:
            errors.append("LIVE_TRADING_ENABLED is false")
        if not self.ibkr_order_submission_enabled:
            errors.append("IBKR_ORDER_SUBMISSION_ENABLED is false")
        if not self.polymarket_order_submission_enabled:
            errors.append("POLYMARKET_ORDER_SUBMISSION_ENABLED is false")
        if not self.operator_approval_id.strip():
            errors.append("OPERATOR_APPROVAL_ID is absent")
        if not self.clob_credentials_configured:
            errors.append("CLOB L2 credentials are absent")
        if self.polymarket_signature_type.upper() == "AUTO" and not self._is_configured(
            self.polymarket_private_key
        ):
            errors.append("wallet classification cannot run without a protected signing key")
        reserves = {
            "MODEL_RISK_RESERVE_USD": self.model_risk_reserve_usd,
            "OPERATIONAL_RISK_RESERVE_USD": self.operational_risk_reserve_usd,
            "EFFR_BASIS_RESERVE_USD": self.effr_basis_reserve_usd,
        }
        errors.extend(f"{name} must be positive" for name, value in reserves.items() if value <= 0)
        return errors


def _read_env_keys(path: Path) -> list[str]:
    keys: list[str] = []
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match:
            keys.append(match.group(1))
    return keys


def validate_environment_schema(env_path: Path, example_path: Path) -> None:
    actual = _read_env_keys(env_path)
    allowed = set(_read_env_keys(example_path))
    duplicates = sorted({key for key in actual if actual.count(key) > 1})
    unknown = sorted(set(actual) - allowed)
    problems: list[str] = []
    if duplicates:
        problems.append(f"duplicate variables: {', '.join(duplicates)}")
    if unknown:
        problems.append(f"unknown variables: {', '.join(unknown)}")
    if problems:
        raise ValueError("invalid .env schema: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_path = Path(".env")
    example_path = Path(".env.example")
    if env_path.exists() and example_path.exists():
        validate_environment_schema(env_path, example_path)
    return Settings(_env_file=env_path)
