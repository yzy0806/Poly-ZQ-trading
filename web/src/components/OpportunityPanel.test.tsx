import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { GateCheck, Opportunity } from '../types'
import { OpportunityPanel } from './OpportunityPanel'

function failedCheck(index: number): GateCheck {
  return {
    code: `GATE_${index}`,
    category: 'RISK',
    label: `Gate ${index}`,
    status: 'FAILED',
    blocking: true,
    actual_value: String(index),
    operator: '>=',
    required_value: '100',
    unit: 'units',
    detail: `Unique failure ${index}`,
    observed_at: '2026-08-28T00:00:00Z',
    passed: false,
  }
}

function opportunityFixture(): Opportunity {
  return {
    direction: 'LONG',
    zq_side: 'BUY',
    zq_price: '96.33',
    contracts: 10,
    token_requirements: { INC25: '4861.50', INC50PLUS: '9723.00' },
    token_prices: {},
    emergency_token_prices: {},
    scenarios: [],
    emergency_scenarios: [],
    passive_minimum_net_profit: null,
    emergency_minimum_net_profit: null,
    minimum_net_profit: null,
    committed_capital: null,
    return_on_capital_bps: null,
    calculation: null,
    hedge_depth: [],
    tradeable: false,
    gate_reasons: [],
    gate_checks: Array.from({ length: 7 }, (_, index) => failedCheck(index + 1)),
    calculated_at: '2026-08-28T00:00:00Z',
  }
}

describe('OpportunityPanel', () => {
  it('renders every blocking gate and only the long-ZQ execution path', () => {
    render(<OpportunityPanel opportunities={[opportunityFixture()]} />)

    expect(screen.getByText('7 BLOCKING GATES')).toBeTruthy()
    for (let index = 1; index <= 7; index += 1) {
      expect(screen.getByText(`Unique failure ${index}`)).toBeTruthy()
    }
    expect(screen.getByText('LONG ZQ')).toBeTruthy()
    expect(screen.getByText('BUY 10 @ 96.33')).toBeTruthy()
    expect(screen.queryByText('SHORT ZQ')).toBeNull()
    expect(screen.queryByText(/SELL 10/)).toBeNull()
  })

  it('shows the exact backend operands behind the waterfall and capital return', () => {
    const opportunity = opportunityFixture()
    const passive = {
      move_bps: 0,
      settlement_price: '96.36875',
      zq_entry_price: '96.30',
      contracts: 10,
      futures_point_value: '4167',
      futures_price_change: '0.06875',
      futures_pnl: '2864.8125',
      inc25_shares: '4861.50',
      inc25_entry_price: '0.51',
      inc25_payout: '0',
      inc25_pnl: '-2479.365',
      inc50plus_shares: '9723.00',
      inc50plus_entry_price: '0.006',
      inc50plus_payout: '0',
      inc50plus_pnl: '-58.338',
      polymarket_pnl: '-2537.703',
      gross_pnl: '327.1095',
      costs: '0',
      reserves: '0',
      net_pnl: '327.1095',
    }
    opportunity.token_prices = { INC25: '0.51', INC50PLUS: '0.006' }
    opportunity.emergency_token_prices = { INC25: '0.52', INC50PLUS: '0.007' }
    opportunity.scenarios = [passive]
    opportunity.emergency_scenarios = [{
      ...passive,
      inc25_entry_price: '0.52',
      inc25_pnl: '-2527.98',
      inc50plus_entry_price: '0.007',
      inc50plus_pnl: '-68.061',
      polymarket_pnl: '-2596.041',
      gross_pnl: '268.7715',
      net_pnl: '268.7715',
    }]
    opportunity.passive_minimum_net_profit = '327.1095'
    opportunity.emergency_minimum_net_profit = '268.7715'
    opportunity.minimum_net_profit = '268.7715'
    opportunity.committed_capital = '8519.00'
    opportunity.return_on_capital_bps = '315.50'
    opportunity.calculation = {
      inc25_shares_per_contract: '486.15',
      inc50plus_shares_per_contract: '972.30',
      inc25_emergency_hedge_cash: '2527.98',
      inc50plus_emergency_hedge_cash: '68.061',
      emergency_hedge_cash: '2596.041',
      incremental_initial_margin: '5922.959',
      emergency_cash_reserve: '0',
      committed_capital: '8519.00',
      costs: {
        ibkr_commission: '0',
        polymarket_fees: '0',
        zq_slippage_reserve: '0',
        polymarket_slippage_reserve: '0',
        rounding_reserve: '0',
        explicit_costs: '0',
        model_reserve: '0',
        operational_reserve: '0',
        effr_basis_reserve: '0',
        reserves: '0',
      },
    }

    render(<OpportunityPanel opportunities={[opportunity]} />)

    expect(screen.getByText('How every profit number is calculated')).toBeTruthy()
    expect(screen.getByText(/10 × \$4,167 × \(96.36875 − 96.3\)/)).toBeTruthy()
    expect(screen.getByText(/4,861.5 × \(0 payout − 0.51 paid\)/)).toBeTruthy()
    expect(screen.getAllByText('Return on capital').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText('3.16%').length).toBeGreaterThanOrEqual(2)
  })
})
