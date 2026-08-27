export type DecimalValue = string | number | null

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
}

export interface BookLevel { price: DecimalValue; size: DecimalValue }

export interface OrderBook {
  token_id: string
  market: string | null
  bids: BookLevel[]
  asks: BookLevel[]
  best_bid: DecimalValue
  best_ask: DecimalValue
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
  executable_long_expected_move_bps: DecimalValue
  executable_short_expected_move_bps: DecimalValue
  lower_step_bps: number | null
  lower_probability: DecimalValue
  upper_step_bps: number | null
  upper_probability: DecimalValue
  bucket_probabilities: Record<string, DecimalValue>
  executable_long_probability: DecimalValue
  executable_short_probability: DecimalValue
  polymarket_probability_sum: DecimalValue
  polymarket_expected_move_bps: DecimalValue
  expected_move_gap_bps: DecimalValue
  fedwatch: FedWatchDiagnostic
  valid: boolean
  reason: string
  calculated_at: string
}

export interface ScenarioPnl {
  move_bps: number
  futures_pnl: DecimalValue
  polymarket_pnl: DecimalValue
  costs: DecimalValue
  reserves: DecimalValue
  net_pnl: DecimalValue
}

export interface Opportunity {
  direction: string
  zq_side: string
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
  tradeable: boolean
  gate_reasons: string[]
  calculated_at: string
}

export interface MarketProbabilityComparison {
  code: string
  label: string
  zq_probability: DecimalValue
  polymarket_bid: DecimalValue
  polymarket_ask: DecimalValue
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

export interface AlertView {
  alert_id: string
  severity: string
  code: string
  message: string
  flashing: boolean
  acknowledged: boolean
  created_at: string
}

export interface HedgeObligation {
  obligation_id: string
  token_id: string
  due_shares: DecimalValue
  confirmed_shares: DecimalValue
  deficit_shares: DecimalValue
}

export interface BatchView {
  batch_id: string | null
  state: string
  zq_order_id: number | null
  original_quantity: number
  filled_quantity: DecimalValue
  remaining_quantity: DecimalValue
  limit_price: DecimalValue
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
  polymarket: VenueHealth
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
  probabilities: ProbabilitySnapshot
  probability_comparisons: MarketProbabilityComparison[]
  opportunities: Opportunity[]
  active_batch: BatchView
  alerts: AlertView[]
  health_messages: string[]
  metadata: Record<string, unknown>
}
