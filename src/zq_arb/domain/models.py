from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .enums import AlertSeverity, BatchState, ConnectionStatus, DataQuality, RunMode, Side


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)


class VenueHealth(StrictModel):
    status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    authenticated: bool = False
    message: str = "not started"
    reconnect_count: int = 0
    last_message_at: datetime | None = None


class Quote(StrictModel):
    instrument: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    last: Decimal | None = None
    source_timestamp: datetime | None = None
    received_at: datetime = Field(default_factory=utc_now)
    quality: DataQuality = DataQuality.UNKNOWN
    contract_id: int | None = None

    def age_ms(self, now: datetime | None = None) -> int:
        anchor = now or utc_now()
        return max(0, int((anchor - self.received_at).total_seconds() * 1_000))


class BookLevel(StrictModel):
    price: Decimal
    size: Decimal


class OrderBook(StrictModel):
    token_id: str
    market: str | None = None
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    negative_risk: bool | None = None
    book_hash: str | None = None
    source: str = "REST"
    stream_synchronized: bool = False
    source_timestamp: datetime | None = None
    last_reconciled_at: datetime | None = None
    received_at: datetime = Field(default_factory=utc_now)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def best_bid(self) -> Decimal | None:
        return max((level.price for level in self.bids), default=None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def best_ask(self) -> Decimal | None:
        return min((level.price for level in self.asks), default=None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def midpoint(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal("2")

    def age_ms(self, now: datetime | None = None) -> int:
        anchor = now or utc_now()
        return max(0, int((anchor - self.received_at).total_seconds() * 1_000))


class AccountMetrics(StrictModel):
    account_fingerprint: str | None = None
    net_liquidation: Decimal | None = None
    total_cash_value: Decimal | None = None
    init_margin: Decimal | None = None
    maintenance_margin: Decimal | None = None
    available_funds: Decimal | None = None
    excess_liquidity: Decimal | None = None
    full_init_margin: Decimal | None = None
    full_maintenance_margin: Decimal | None = None
    full_available_funds: Decimal | None = None
    full_excess_liquidity: Decimal | None = None
    cushion: Decimal | None = None
    daily_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    futures_pnl: Decimal | None = None
    received_at: datetime | None = None


class FedWatchDiagnostic(StrictModel):
    rates: dict[str, Decimal] = Field(default_factory=dict)
    september_start_effr: Decimal | None = None
    september_end_effr: Decimal | None = None
    october_start_effr: Decimal | None = None
    expected_move_bps: Decimal | None = None
    expected_steps: Decimal | None = None
    lower_step_bps: int | None = None
    lower_probability: Decimal | None = None
    upper_step_bps: int | None = None
    upper_probability: Decimal | None = None
    bucket_probabilities: dict[str, Decimal] = Field(default_factory=dict)
    september_residual_bps: Decimal | None = None
    valid: bool = False
    reason: str = "awaiting four reference quotes"


class ProbabilitySnapshot(StrictModel):
    target_contract_month: str | None = None
    target_bid: Decimal | None = None
    target_ask: Decimal | None = None
    target_mid: Decimal | None = None
    pre_meeting_effr: Decimal | None = None
    post_decision_weight: Decimal | None = None
    implied_average_effr_bid: Decimal | None = None
    implied_average_effr_ask: Decimal | None = None
    implied_average_effr_mid: Decimal | None = None
    expected_move_bps: Decimal | None = None
    executable_long_expected_move_bps: Decimal | None = None
    executable_short_expected_move_bps: Decimal | None = None
    lower_step_bps: int | None = None
    lower_probability: Decimal | None = None
    upper_step_bps: int | None = None
    upper_probability: Decimal | None = None
    bucket_probabilities: dict[str, Decimal] = Field(default_factory=dict)
    executable_long_probability: Decimal | None = None
    executable_short_probability: Decimal | None = None
    polymarket_probability_sum: Decimal | None = None
    polymarket_expected_move_bps: Decimal | None = None
    expected_move_gap_bps: Decimal | None = None
    fedwatch: FedWatchDiagnostic = Field(default_factory=FedWatchDiagnostic)
    valid: bool = False
    reason: str = "awaiting target and pre-meeting anchor quotes"
    calculated_at: datetime = Field(default_factory=utc_now)


class ScenarioPnl(StrictModel):
    move_bps: int
    futures_pnl: Decimal
    polymarket_pnl: Decimal
    costs: Decimal
    reserves: Decimal
    net_pnl: Decimal


class Opportunity(StrictModel):
    direction: str
    zq_side: Side
    zq_price: Decimal | None = None
    contracts: int
    token_requirements: dict[str, Decimal] = Field(default_factory=dict)
    token_prices: dict[str, Decimal] = Field(default_factory=dict)
    emergency_token_prices: dict[str, Decimal] = Field(default_factory=dict)
    scenarios: tuple[ScenarioPnl, ...] = ()
    emergency_scenarios: tuple[ScenarioPnl, ...] = ()
    passive_minimum_net_profit: Decimal | None = None
    emergency_minimum_net_profit: Decimal | None = None
    minimum_net_profit: Decimal | None = None
    committed_capital: Decimal | None = None
    return_on_capital_bps: Decimal | None = None
    tradeable: bool = False
    gate_reasons: tuple[str, ...] = ()
    calculated_at: datetime = Field(default_factory=utc_now)


class MarketProbabilityComparison(StrictModel):
    code: str
    label: str
    zq_probability: Decimal | None = None
    polymarket_bid: Decimal | None = None
    polymarket_ask: Decimal | None = None
    polymarket_mid: Decimal | None = None
    midpoint_gap: Decimal | None = None
    book_age_ms: int | None = None
    stream_synchronized: bool = False
    mapping_verified: bool = False


class HedgeObligationView(StrictModel):
    obligation_id: str
    batch_id: str
    exec_id: str
    token_id: str
    due_shares: Decimal
    confirmed_shares: Decimal = Decimal("0")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def deficit_shares(self) -> Decimal:
        return max(Decimal("0"), self.due_shares - self.confirmed_shares)


class BatchView(StrictModel):
    batch_id: str | None = None
    state: BatchState = BatchState.IDLE
    zq_order_id: int | None = None
    original_quantity: int = 10
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    limit_price: Decimal | None = None
    obligations: tuple[HedgeObligationView, ...] = ()
    updated_at: datetime = Field(default_factory=utc_now)


class AlertView(StrictModel):
    alert_id: str
    severity: AlertSeverity
    code: str
    message: str
    flashing: bool = False
    acknowledged: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class MarketMappingStatus(StrictModel):
    verified: bool = False
    rule_hash_match: bool = False
    market_count_match: bool = False
    checked_at: datetime | None = None
    errors: tuple[str, ...] = ()


class EligibilityStatus(StrictModel):
    checked: bool = False
    blocked: bool | None = None
    country: str | None = None
    permitted_for_live: bool = False
    checked_at: datetime | None = None
    reason: str = "not checked"


class EngineSnapshot(StrictModel):
    snapshot_id: int = 0
    generated_at: datetime = Field(default_factory=utc_now)
    software_version: str
    config_version: str
    strategy_version: str
    run_mode: RunMode = RunMode.READ_ONLY
    armed: bool = False
    paused: bool = False
    kill_switch: bool = False
    ibkr: VenueHealth = Field(default_factory=VenueHealth)
    polymarket: VenueHealth = Field(default_factory=VenueHealth)
    eligibility: EligibilityStatus = Field(default_factory=EligibilityStatus)
    mapping: MarketMappingStatus = Field(default_factory=MarketMappingStatus)
    quotes: dict[str, Quote] = Field(default_factory=dict)
    books: dict[str, OrderBook] = Field(default_factory=dict)
    account: AccountMetrics = Field(default_factory=AccountMetrics)
    probabilities: ProbabilitySnapshot = Field(default_factory=ProbabilitySnapshot)
    probability_comparisons: tuple[MarketProbabilityComparison, ...] = ()
    opportunities: tuple[Opportunity, ...] = ()
    active_batch: BatchView = Field(default_factory=BatchView)
    alerts: tuple[AlertView, ...] = ()
    health_messages: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
