import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { EngineSnapshot, GateCheck } from '../types'
import { ProbabilityPanel } from './ProbabilityPanel'

function failedSubscription(): GateCheck {
  return {
    code: 'ZQU6_GENERATION',
    category: 'CROSS_VENUE',
    label: 'ZQU6 subscription generation',
    status: 'FAILED',
    blocking: true,
    actual_value: '3',
    operator: '==',
    required_value: '4',
    unit: null,
    detail: 'ZQU6 subscription generation is not current',
    observed_at: '2026-08-28T00:00:00Z',
    passed: false,
  }
}

function stateFixture(): EngineSnapshot {
  return {
    quotes: {},
    effr: {
      source: 'NYFED_API',
      rate_percent: '3.6313',
      effective_date: '2026-08-27',
      fetched_at: '2026-08-28T00:00:00Z',
      target_rate_from: '3.50',
      target_rate_to: '3.75',
      revision_indicator: '',
      valid: true,
      reason: 'New York Fed official EFFR',
    },
    probability_comparisons: [{
      code: 'INC25',
      label: 'Increase 25',
      zq_probability: '0.3536',
      polymarket_bid: '0.51',
      polymarket_bid_size: '12345',
      polymarket_ask: '0.52',
      polymarket_ask_size: '6789',
      polymarket_mid: '0.515',
      midpoint_gap: '-0.1614',
      book_age_ms: 20,
      stream_synchronized: true,
      mapping_verified: true,
    }],
    probabilities: {
      target_contract_month: '202609',
      target_bid: '96.325',
      target_ask: '96.330',
      target_mid: '96.3275',
      pre_meeting_effr: '3.6313',
      post_decision_weight: '0.4667',
      implied_average_effr_mid: '3.6725',
      expected_move_bps: '8.84',
      executable_buy_expected_move_bps: '8.30',
      bid_reference_expected_move_bps: '9.38',
      lower_step_bps: 0,
      lower_probability: '0.6464',
      upper_step_bps: 25,
      upper_probability: '0.3536',
      bucket_probabilities: {},
      polymarket_probability_sum: '1.0',
      polymarket_expected_move_bps: '7.18',
      expected_move_gap_bps: '1.66',
      fedwatch: { valid: false, reason: 'diagnostic only' },
      valid: true,
      analytics_qualified: false,
      execution_qualified: false,
      qualification_reason: 'NOT EXECUTION-QUALIFIED — 1 of 16 execution checks failed',
      qualification_checks: [failedSubscription()],
      reason: 'direct calculation',
      calculated_at: '2026-08-28T00:00:00Z',
    },
  } as unknown as EngineSnapshot
}

describe('ProbabilityPanel', () => {
  it('shows the exact failed qualification against its configured threshold', () => {
    render(<ProbabilityPanel state={stateFixture()} />)

    expect(screen.getByText(/NOT EXECUTION-QUALIFIED/)).toBeTruthy()
    expect(screen.getByText('ZQU6 subscription generation')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('== 4')).toBeTruthy()
    expect(screen.getByText('ZQU6 subscription generation is not current')).toBeTruthy()
    expect(screen.getByText(/LONG-ONLY ENTRY MOVE/)).toBeTruthy()
    expect(screen.getByText('Bid size')).toBeTruthy()
    expect(screen.getByText('12,345')).toBeTruthy()
    expect(screen.getByText('Ask size')).toBeTruthy()
    expect(screen.getByText('6,789')).toBeTruthy()
  })
})
