import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AccountMetrics, MarginPreview, ReconciliationStatus } from '../types'
import { RiskPanel } from './RiskPanel'

describe('RiskPanel', () => {
  it('withholds an unqualified projection and exposes audited manual controls', () => {
    const onControl = vi.fn()
    const account = {
      net_liquidation: '100000',
      full_excess_liquidity: '90000',
      cushion: '0.9',
    } as AccountMetrics
    const preview = {
      status: 'AVAILABLE',
      qualification_status: 'REFRESH_REQUIRED',
      qualified_for_next_batch: false,
      qualification_detail: 'automatic refresh required',
      next_batch_initial_margin: '5923',
      projected_excess_liquidity: '84077',
      side: 'BUY',
      available: true,
    } as MarginPreview
    const reconciliation = {
      clean: false,
      method: 'NOT_CONFIRMED',
      reason: 'manual venue reconciliation has not been confirmed',
    } as ReconciliationStatus
    render(
      <RiskPanel
        account={account}
        preview={preview}
        reconciliation={reconciliation}
        onControl={onControl}
      />,
    )

    expect(screen.getByText('REFRESH REQUIRED')).toBeTruthy()
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByText('Confirm venues reconciled'))
    expect(onControl).toHaveBeenCalledWith('CONFIRM_RECONCILED')
  })
})
