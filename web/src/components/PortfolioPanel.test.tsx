import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PortfolioView } from '../types'
import { PortfolioPanel } from './PortfolioPanel'

describe('PortfolioPanel', () => {
  it('shows cross-venue quantities, conservative marks, and combined pnl', () => {
    const portfolio: PortfolioView = {
      positions: [
        {
          venue: 'IBKR', instrument: '202609', label: 'ZQ 202609', strategy_quantity: '3', venue_quantity: '3', average_entry_price: '96.295', mark_price: '96.2925', mark_source: 'IBKR LIVE BEST BID', multiplier: '4167', cost_basis: null, market_value: null, unrealized_pnl: '-31.2525', simulated: false, reconciled: true, mark_updated_at: new Date().toISOString(),
        },
        {
          venue: 'POLYMARKET', instrument: 'inc25-token', label: 'INC25 YES', strategy_quantity: '1458.45', venue_quantity: '1458.45', average_entry_price: '0.56', mark_price: '0.57', mark_source: 'POLYMARKET LIVE BEST BID', multiplier: '1', cost_basis: '816.732', market_value: '831.3165', unrealized_pnl: '14.5845', simulated: true, reconciled: true, mark_updated_at: new Date().toISOString(),
        },
      ],
      zq_unrealized_pnl: '-31.2525',
      polymarket_unrealized_pnl: '14.5845',
      combined_unrealized_pnl: '-16.668',
      valuation_complete: true,
      valuation_reason: 'all strategy positions marked to executable best bids',
      valued_at: new Date().toISOString(),
    }

    render(<PortfolioPanel portfolio={portfolio} />)

    expect(screen.getByText('Current cross-venue portfolio')).toBeTruthy()
    expect(screen.getByText('ZQ 202609')).toBeTruthy()
    expect(screen.getByText('INC25 YES')).toBeTruthy()
    expect(screen.getByText('SIMULATED POSITION')).toBeTruthy()
    expect(screen.getByText(/16\.67/)).toBeTruthy()
    expect(screen.getAllByText('MATCH')).toHaveLength(2)
  })
})
