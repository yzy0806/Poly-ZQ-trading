export type DecimalValue = string | number | null

export interface GateCheck {
  code: string
  category: string
  label: string
  status: 'PASSED' | 'FAILED' | 'UNAVAILABLE' | 'NOT_APPLICABLE'
  blocking: boolean
  actual_value: string | null
  operator: string | null
  required_value: string | null
  unit: string | null
  detail: string
  observed_at: string
  passed: boolean
}

export interface HedgeDepthView {
  leg_code: string
  required_shares: DecimalValue
  available_shares: DecimalValue
  shortfall_shares: DecimalValue
  price_cap: DecimalValue
  marketable_limit_price: DecimalValue
  best_ask_shares: DecimalValue
  emergency_vwap: DecimalValue
  worst_price: DecimalValue
  sufficient: boolean
}

export interface VenueHealth {
  status: string
  authenticated: boolean
  message: string
  reconnect_count: number
  last_message_at: string | null
}

export interface Quote {
  instrument: string
  bid: DecimalValue
  ask: DecimalValue
  bid_size: DecimalValue
  ask_size: DecimalValue
  last: DecimalValue
  received_at: string
  quality: string
  role: string
  last_price_change_at: string | null
  last_market_data_event_at: string | null
  market_data_type: number | null
  subscription_status: string
  subscription_generation: number
  farm_status: string
  analytics_qualified: boolean
  pretrade_qualified: boolean
  validation_reason: string
}

export interface IbkrFarmHealth {
  name: string
  service: string
  status: string
  message: string
  current: boolean
  last_changed_at: string | null
}

export interface EffrObservation {
  source: 'NYFED_API' | 'MANUAL'
  rate_percent: DecimalValue
  effective_date: string | null
  fetched_at: string | null
  target_rate_from: DecimalValue
  target_rate_to: DecimalValue
  revision_indicator: string
  valid: boolean
  reason: string
}

export interface BookLevel { price: DecimalValue; size: DecimalValue }

export interface OrderBook {
  token_id: string
  market: string | null
  bids: BookLevel[]
  asks: BookLevel[]
  best_bid: DecimalValue
  best_bid_size: DecimalValue
  best_ask: DecimalValue
  best_ask_size: DecimalValue
  midpoint: DecimalValue
  source: string
  stream_synchronized: boolean
  last_reconciled_at: string | null
  received_at: string
}

export interface FedWatchDiagnostic {
  rates: Record<string, DecimalValue>
  september_start_effr: DecimalValue
  september_end_effr: DecimalValue
  october_start_effr: DecimalValue
  expected_move_bps: DecimalValue
  expected_steps: DecimalValue
  lower_step_bps: number | null
  lower_probability: DecimalValue
  upper_step_bps: number | null
  upper_probability: DecimalValue
  bucket_probabilities: Record<string, DecimalValue>
  september_residual_bps: DecimalValue
  valid: boolean
  reason: string
}

export interface ProbabilitySnapshot {
  target_contract_month: string | null
  target_bid: DecimalValue
  target_ask: DecimalValue
  target_mid: DecimalValue
  pre_meeting_effr: DecimalValue
  post_decision_weight: DecimalValue
  implied_average_effr_bid: DecimalValue
  implied_average_effr_ask: DecimalValue
  implied_average_effr_mid: DecimalValue
  expected_move_bps: DecimalValue
  executable_buy_expected_move_bps: DecimalValue
  bid_reference_expected_move_bps: DecimalValue
  lower_step_bps: number | null
  lower_probability: DecimalValue
  upper_step_bps: number | null
  upper_probability: DecimalValue
  bucket_probabilities: Record<string, DecimalValue>
  executable_buy_probability: DecimalValue
  bid_reference_probability: DecimalValue
  polymarket_probability_sum: DecimalValue
  polymarket_expected_move_bps: DecimalValue
  expected_move_gap_bps: DecimalValue
  fedwatch: FedWatchDiagnostic
  valid: boolean
  analytics_qualified: boolean
  execution_qualified: boolean
  qualification_reason: string
  qualification_checks?: GateCheck[]
  reason: string
  calculated_at: string
}

export interface ScenarioPnl {
  move_bps: number
  settlement_price: DecimalValue
  zq_entry_price: DecimalValue
  contracts: number
  futures_point_value: DecimalValue
  futures_price_change: DecimalValue
  futures_pnl: DecimalValue
  inc25_shares: DecimalValue
  inc25_entry_price: DecimalValue
  inc25_payout: DecimalValue
  inc25_pnl: DecimalValue
  inc50plus_shares: DecimalValue
  inc50plus_entry_price: DecimalValue
  inc50plus_payout: DecimalValue
  inc50plus_pnl: DecimalValue
  polymarket_pnl: DecimalValue
  gross_pnl: DecimalValue
  costs: DecimalValue
  reserves: DecimalValue
  net_pnl: DecimalValue
}

export interface OpportunityCostBreakdown {
  ibkr_commission: DecimalValue
  polymarket_fees: DecimalValue
  zq_slippage_reserve: DecimalValue
  polymarket_slippage_reserve: DecimalValue
  rounding_reserve: DecimalValue
  explicit_costs: DecimalValue
  model_reserve: DecimalValue
  operational_reserve: DecimalValue
  effr_basis_reserve: DecimalValue
  reserves: DecimalValue
}

export interface OpportunityCalculation {
  inc25_shares_per_contract: DecimalValue
  inc50plus_shares_per_contract: DecimalValue
  inc25_emergency_hedge_cash: DecimalValue
  inc50plus_emergency_hedge_cash: DecimalValue
  emergency_hedge_cash: DecimalValue
  incremental_initial_margin: DecimalValue
  emergency_cash_reserve: DecimalValue
  committed_capital: DecimalValue
  costs: OpportunityCostBreakdown
}

export interface Opportunity {
  direction: 'LONG'
  zq_side: 'BUY'
  zq_price: DecimalValue
  contracts: number
  token_requirements: Record<string, DecimalValue>
  token_prices: Record<string, DecimalValue>
  emergency_token_prices: Record<string, DecimalValue>
  scenarios: ScenarioPnl[]
  emergency_scenarios: ScenarioPnl[]
  passive_minimum_net_profit: DecimalValue
  emergency_minimum_net_profit: DecimalValue
  minimum_net_profit: DecimalValue
  committed_capital: DecimalValue
  return_on_capital_bps: DecimalValue
  calculation: OpportunityCalculation | null
  hedge_depth: HedgeDepthView[]
  tradeable: boolean
  gate_reasons: string[]
  gate_checks?: GateCheck[]
  calculated_at: string
}

export interface MarketProbabilityComparison {
  code: string
  label: string
  zq_probability: DecimalValue
  polymarket_bid: DecimalValue
  polymarket_bid_size: DecimalValue
  polymarket_ask: DecimalValue
  polymarket_ask_size: DecimalValue
  polymarket_mid: DecimalValue
  midpoint_gap: DecimalValue
  book_age_ms: number | null
  stream_synchronized: boolean
  mapping_verified: boolean
}

export interface AccountMetrics {
  net_liquidation: DecimalValue
  total_cash_value: DecimalValue
  init_margin: DecimalValue
  maintenance_margin: DecimalValue
  available_funds: DecimalValue
  excess_liquidity: DecimalValue
  full_init_margin: DecimalValue
  full_maintenance_margin: DecimalValue
  full_available_funds: DecimalValue
  full_excess_liquidity: DecimalValue
  cushion: DecimalValue
  daily_pnl: DecimalValue
  unrealized_pnl: DecimalValue
  realized_pnl: DecimalValue
  futures_pnl: DecimalValue
  received_at: string | null
}

export interface MarginPreview {
  status: 'NOT_REQUESTED' | 'PENDING' | 'AVAILABLE' | 'FAILED'
  order_id: number | null
  contract_month: string | null
  side: 'BUY'
  quantity: number | null
  limit_price: DecimalValue
  init_margin_change: DecimalValue
  init_margin_after: DecimalValue
  maintenance_margin_change: DecimalValue
  maintenance_margin_after: DecimalValue
  equity_with_loan_after: DecimalValue
  commission: DecimalValue
  commission_currency: string | null
  warning_text: string | null
  error: string | null
  requested_at: string | null
  received_at: string | null
  available: boolean
  next_batch_initial_margin: DecimalValue
  projected_excess_liquidity: DecimalValue
  qualification_status: 'NOT_REQUESTED' | 'REFRESHING' | 'CURRENT' | 'REFRESH_REQUIRED' | 'FAILED'
  qualified_for_next_batch: boolean
  qualification_detail: string
  qualification_age_seconds: number | null
}

export interface ReconciliationStatus {
  clean: boolean
  method: string
  confirmed_by: string | null
  confirmed_at: string | null
  confirmed_snapshot_id: number | null
  reason: string
  invalidated_at: string | null
}

export interface StrategyRisk {
  allocated_capital: DecimalValue
  cumulative_realized_pnl: DecimalValue
  unrealized_pnl: DecimalValue
  fees: DecimalValue
  equity: DecimalValue
  high_water_mark: DecimalValue
  drawdown: DecimalValue
  daily_pnl: DecimalValue
  trading_day: string | null
  source: string
  valued_at: string
}

export interface PortfolioPosition {
  venue: 'IBKR' | 'POLYMARKET'
  instrument: string
  label: string
  strategy_quantity: DecimalValue
  venue_quantity: DecimalValue
  average_entry_price: DecimalValue
  mark_price: DecimalValue
  mark_source: string
  multiplier: DecimalValue
  cost_basis: DecimalValue
  market_value: DecimalValue
  unrealized_pnl: DecimalValue
  simulated: boolean
  reconciled: boolean | null
  mark_updated_at: string | null
}

export interface PortfolioView {
  positions: PortfolioPosition[]
  zq_unrealized_pnl: DecimalValue
  polymarket_unrealized_pnl: DecimalValue
  combined_unrealized_pnl: DecimalValue
  valuation_complete: boolean
  valuation_reason: string
  valued_at: string
}

export interface AlertView {
  alert_id: string
  severity: string
  code: string
  message: string
  flashing: boolean
  acknowledged: boolean
  resolved: boolean
  created_at: string
  resolved_at: string | null
}

export interface HedgeObligation {
  obligation_id: string
  token_id: string
  due_shares: DecimalValue
  confirmed_shares: DecimalValue
  deficit_shares: DecimalValue
  state: string
  latest_order_id: string | null
  latest_limit_price: DecimalValue
  reprice_count: number
}

export interface BatchView {
  batch_id: string | null
  state: string
  zq_order_id: number | null
  original_quantity: number
  filled_quantity: DecimalValue
  remaining_quantity: DecimalValue
  limit_price: DecimalValue
  zq_order_status: string | null
  cancel_reason: string | null
  residual_minimum_net_profit: DecimalValue
  residual_required_profit: DecimalValue
  residual_return_on_capital_bps: DecimalValue
  obligations: HedgeObligation[]
  updated_at: string
}

export interface EngineSnapshot {
  snapshot_id: number
  generated_at: string
  software_version: string
  config_version: string
  strategy_version: string
  run_mode: string
  armed: boolean
  paused: boolean
  kill_switch: boolean
  ibkr: VenueHealth
  ibkr_farms: Record<string, IbkrFarmHealth>
  polymarket: VenueHealth
  effr: EffrObservation
  eligibility: {
    checked: boolean
    blocked: boolean | null
    country: string | null
    permitted_for_live: boolean
    reason: string
  }
  mapping: {
    verified: boolean
    rule_hash_match: boolean
    market_count_match: boolean
    checked_at: string | null
    errors: string[]
  }
  quotes: Record<string, Quote>
  books: Record<string, OrderBook>
  account: AccountMetrics
  margin_preview: MarginPreview
  reconciliation: ReconciliationStatus
  strategy_risk: StrategyRisk
  portfolio: PortfolioView
  probabilities: ProbabilitySnapshot
  probability_comparisons: MarketProbabilityComparison[]
  opportunities: Opportunity[]
  active_batch: BatchView
  alerts: AlertView[]
  health_messages: string[]
  metadata: Record<string, unknown>
}
