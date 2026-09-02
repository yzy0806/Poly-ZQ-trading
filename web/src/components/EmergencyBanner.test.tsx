import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AlertView } from '../types'
import { EmergencyBanner } from './EmergencyBanner'

function alert(code: string): AlertView {
  return {
    alert_id: 'alert-1',
    severity: 'CRITICAL',
    code,
    message: 'test alert',
    flashing: true,
    acknowledged: false,
    resolved: false,
    created_at: '2026-09-02T09:24:24Z',
    resolved_at: null,
  }
}

describe('EmergencyBanner', () => {
  it('does not label a general analytics failure as unhedged exposure', () => {
    render(<EmergencyBanner alert={alert('ANALYTICS_FAILED')} acknowledge={vi.fn()} />)

    expect(screen.getByText('CRITICAL SYSTEM ALERT — MANUAL ACTION REQUIRED')).toBeTruthy()
  })

  it('retains the unhedged warning for an actual hedge-routing failure', () => {
    render(<EmergencyBanner alert={alert('HEDGE_ROUTING_FAILED')} acknowledge={vi.fn()} />)

    expect(screen.getByText('UNHEDGED ZQ — MANUAL ACTION REQUIRED')).toBeTruthy()
  })
})
