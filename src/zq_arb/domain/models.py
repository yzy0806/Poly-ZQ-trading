from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .enums import (
    AlertSeverity,
    BatchState,
    ConnectionStatus,
    DataQuality,
    FarmStatus,
    GateStatus,
    MarginPreviewStatus,
    MarginQualificationStatus,
    QuoteRole,
    RunMode,
    Side,
    SubscriptionStatus,
)


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
    role: QuoteRole = QuoteRole.DIAGNOSTIC
    last_price_change_at: datetime | None = None
    last_market_data_event_at: datetime | None = None
    market_data_type: int | None = None
    subscription_status: SubscriptionStatus = SubscriptionStatus.PENDING
    subscription_generation: int = 0
    farm_status: FarmStatus = FarmStatus.UNKNOWN
    analytics_qualified: bool = False
    pretrade_qualified: bool = False
    validation_reason: str = "awaiting live subscription qualification"

    def age_ms(self, now: datetime | None = None) -> int:
        """Compatibility alias for economic price-change age."""

        return self.price_change_age_ms(now)

    def price_change_age_ms(self, now: datetime | None = None) -> int:
        anchor = now or utc_now()
        timestamp = self.last_price_change_at or self.received_at
        return max(0, (anchor - timestamp) // timedelta(milliseconds=1))

    @property
    def has_valid_two_sided_market(self) -> bool:
        return bool(
            self.bid is not None
            and self.ask is not None
            and self.bid > 0
            and self.ask > 0
            and self.bid <= self.ask
        )


class IbkrFarmHealth(StrictModel):
    name: str
    service: str
    status: FarmStatus = FarmStatus.UNKNOWN
    message: str = "not observed"
    current: bool = False
    last_changed_at: datetime | None = None


class EffrObservation(StrictModel):
    source: Literal["NYFED_API", "MANUAL"] = "NYFED_API"
    rate_percent: Decimal | None = None
    effective_date: date | None = None
    fetched_at: datetime | None = None
    target_rate_from: Decimal | None = None
    target_rate_to: Decimal | None = None
    revision_indicator: str = ""
    valid: bool = False
    reason: str = "awaiting New York Fed EFFR"


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
    def best_bid_size(self) -> Decimal | None:
        best_bid = self.best_bid
        if best_bid is None:
            return None
        return sum(
            (level.size for level in self.bids if level.price == best_bid),
            start=Decimal("0"),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def best_ask_size(self) -> Decimal | None:
        best_ask = self.best_ask
        if best_ask is None:
            return None
        return sum(
            (level.size for level in self.asks if level.price == best_ask),
            start=Decimal("0"),
        )

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


class MarginPreview(StrictModel):
    status: MarginPreviewStatus = MarginPreviewStatus.NOT_REQUESTED
    order_id: int | None = None
    contract_month: str | None = None
    side: Literal[Side.BUY] = Side.BUY
    quantity: int | None = None
    limit_price: Decimal | None = None
    init_margin_before: Decimal | None = None
    init_margin_change: Decimal | None = None
    init_margin_after: Decimal | None = None
    maintenance_margin_before: Decimal | None = None
    maintenance_margin_change: Decimal | None = None
    maintenance_margin_after: Decimal | None = None
    equity_with_loan_before: Decimal | None = None
    equity_with_loan_change: Decimal | None = None
    equity_with_loan_after: Decimal | None = None
    commission: Decimal | None = None
    commission_currency: str | None = None
    warning_text: str | None = None
    error: str | None = None
    requested_at: datetime | None = None
    received_at: datetime | None = None
    qualification_status: MarginQualificationStatus = MarginQualificationStatus.NOT_REQUESTED
    qualified_for_next_batch: bool = False
    qualification_detail: str = "IBKR BUY-10 what-if preview has not been requested"
    qualification_age_seconds: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available(self) -> bool:
        return self.status is MarginPreviewStatus.AVAILABLE and self.init_margin_change is not None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_batch_initial_margin(self) -> Decimal | None:
        if not self.available or self.init_margin_change is None:
            return None
        return max(Decimal("0"), self.init_margin_change)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def projected_excess_liquidity(self) -> Decimal | None:
        if self.equity_with_loan_after is None or self.init_margin_after is None:
            return None
        return self.equity_with_loan_after - self.init_margin_after

    def age_seconds(self, now: datetime | None = None) -> int | None:
        if self.received_at is None:
            return None
        anchor = now or utc_now()
        return max(0, int((anchor - self.received_at).total_seconds()))


class GateCheck(StrictModel):
    code: str
    category: str
    label: str
    status: GateStatus
    blocking: bool = True
    actual_value: str | None = None
    operator: str | None = None
    required_value: str | None = None
    unit: str | None = None
    detail: str
    observed_at: datetime = Field(default_factory=utc_now)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASSED


class HedgeDepthView(StrictModel):
    leg_code: str
    required_shares: Decimal
    available_shares: Decimal
    shortfall_shares: Decimal
    price_cap: Decimal
    marketable_limit_price: Decimal | None = None
    best_ask_shares: Decimal = Decimal("0")
    emergency_vwap: Decimal | None = None
    worst_price: Decimal | None = None
    sufficient: bool = False


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
    reason: str = "awaiting September-November reference quotes"


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
    executable_buy_expected_move_bps: Decimal | None = None
    bid_reference_expected_move_bps: Decimal | None = None
    lower_step_bps: int | None = None
    lower_probability: Decimal | None = None
    upper_step_bps: int | None = None
    upper_probability: Decimal | None = None
    bucket_probabilities: dict[str, Decimal] = Field(default_factory=dict)
    executable_buy_probability: Decimal | None = None
    bid_reference_probability: Decimal | None = None
    polymarket_probability_sum: Decimal | None = None
    polymarket_expected_move_bps: Decimal | None = None
    expected_move_gap_bps: Decimal | None = None
    fedwatch: FedWatchDiagnostic = Field(default_factory=FedWatchDiagnostic)
    valid: bool = False
    analytics_qualified: bool = False
    execution_qualified: bool = False
    qualification_reason: str = "NOT EXECUTION-QUALIFIED"
    qualification_checks: tuple[GateCheck, ...] = ()
    reason: str = "awaiting target quote and validated pre-meeting EFFR"
    calculated_at: datetime = Field(default_factory=utc_now)


class ScenarioPnl(StrictModel):
    move_bps: int
    settlement_price: Decimal
    zq_entry_price: Decimal
    contracts: int
    futures_point_value: Decimal
    futures_price_change: Decimal
    futures_pnl: Decimal
    inc25_shares: Decimal
    inc25_entry_price: Decimal
    inc25_payout: Decimal
    inc25_pnl: Decimal
    inc50plus_shares: Decimal
    inc50plus_entry_price: Decimal
    inc50plus_payout: Decimal
    inc50plus_pnl: Decimal
    polymarket_pnl: Decimal
    gross_pnl: Decimal
    costs: Decimal | None
    net_pnl: Decimal | None


class OpportunityCostBreakdown(StrictModel):
    ibkr_commission: Decimal
    polymarket_fees: Decimal | None
    explicit_costs: Decimal | None


class OpportunityCalculation(StrictModel):
    inc25_shares_per_contract: Decimal
    inc50plus_shares_per_contract: Decimal
    inc25_emergency_hedge_cash: Decimal
    inc50plus_emergency_hedge_cash: Decimal
    emergency_hedge_cash: Decimal
    incremental_initial_margin: Decimal | None
    emergency_cash_reserve: Decimal
    committed_capital: Decimal | None
    costs: OpportunityCostBreakdown


class Opportunity(StrictModel):
    direction: Literal["LONG"] = "LONG"
    zq_side: Literal[Side.BUY] = Side.BUY
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
    calculation: OpportunityCalculation | None = None
    hedge_depth: tuple[HedgeDepthView, ...] = ()
    tradeable: bool = False
    gate_reasons: tuple[str, ...] = ()
    gate_checks: tuple[GateCheck, ...] = ()
    calculated_at: datetime = Field(default_factory=utc_now)


class MarketProbabilityComparison(StrictModel):
    code: str
    label: str
    zq_probability: Decimal | None = None
    polymarket_bid: Decimal | None = None
    polymarket_bid_size: Decimal | None = None
    polymarket_ask: Decimal | None = None
    polymarket_ask_size: Decimal | None = None
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
    state: str = "PENDING"
    latest_order_id: str | None = None
    latest_limit_price: Decimal | None = None
    reprice_count: int = 0

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
    zq_order_status: str | None = None
    cancel_reason: str | None = None
    residual_minimum_net_profit: Decimal | None = None
    residual_required_profit: Decimal | None = None
    residual_return_on_capital_bps: Decimal | None = None
    obligations: tuple[HedgeObligationView, ...] = ()
    updated_at: datetime = Field(default_factory=utc_now)


class AlertView(StrictModel):
    alert_id: str
    severity: AlertSeverity
    code: str
    message: str
    flashing: bool = False
    acknowledged: bool = False
    resolved: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None


class ReconciliationStatusView(StrictModel):
    clean: bool = False
    method: str = "NOT_CONFIRMED"
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    confirmed_snapshot_id: int | None = None
    reason: str = "manual venue reconciliation has not been confirmed"
    invalidated_at: datetime | None = None


class PortfolioPositionView(StrictModel):
    venue: Literal["IBKR", "POLYMARKET"]
    instrument: str
    label: str
    strategy_quantity: Decimal = Decimal("0")
    venue_quantity: Decimal | None = None
    average_entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    mark_source: str = "UNAVAILABLE"
    multiplier: Decimal = Decimal("1")
    cost_basis: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    simulated: bool = False
    reconciled: bool | None = None
    mark_updated_at: datetime | None = None


class PortfolioView(StrictModel):
    positions: tuple[PortfolioPositionView, ...] = ()
    zq_unrealized_pnl: Decimal | None = Decimal("0")
    polymarket_unrealized_pnl: Decimal | None = Decimal("0")
    combined_unrealized_pnl: Decimal | None = Decimal("0")
    valuation_complete: bool = True
    valuation_reason: str = "no open strategy positions"
    valued_at: datetime = Field(default_factory=utc_now)


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
    ibkr_farms: dict[str, IbkrFarmHealth] = Field(default_factory=dict)
    polymarket: VenueHealth = Field(default_factory=VenueHealth)
    effr: EffrObservation = Field(default_factory=EffrObservation)
    eligibility: EligibilityStatus = Field(default_factory=EligibilityStatus)
    mapping: MarketMappingStatus = Field(default_factory=MarketMappingStatus)
    quotes: dict[str, Quote] = Field(default_factory=dict)
    books: dict[str, OrderBook] = Field(default_factory=dict)
    account: AccountMetrics = Field(default_factory=AccountMetrics)
    margin_preview: MarginPreview = Field(default_factory=MarginPreview)
    reconciliation: ReconciliationStatusView = Field(default_factory=ReconciliationStatusView)
    portfolio: PortfolioView = Field(default_factory=PortfolioView)
    probabilities: ProbabilitySnapshot = Field(default_factory=ProbabilitySnapshot)
    probability_comparisons: tuple[MarketProbabilityComparison, ...] = ()
    opportunities: tuple[Opportunity, ...] = ()
    active_batch: BatchView = Field(default_factory=BatchView)
    alerts: tuple[AlertView, ...] = ()
    health_messages: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
