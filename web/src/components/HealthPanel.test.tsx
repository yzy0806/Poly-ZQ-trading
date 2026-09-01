import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { EngineSnapshot } from '../types'
import { HealthPanel } from './HealthPanel'

function stateFixture(): EngineSnapshot {
  return {
    ibkr: {
      status: 'CONNECTED',
      authenticated: true,
      message: 'TWS connected',
      reconnect_count: 0,
      last_message_at: '2026-08-27T09:00:00Z',
    },
    ibkr_farms: {
      US_FUTURES: {
        name: 'usfuture',
        service: 'MARKET_DATA',
        status: 'CONNECTED',
        message: 'Market data farm connection is OK:usfuture',
        current: false,
        last_changed_at: '2026-08-27T09:00:00Z',
      },
    },
    polymarket: {
      status: 'CONNECTED',
      authenticated: false,
      message: 'market WebSocket connected',
      reconnect_count: 0,
      last_message_at: '2026-08-27T09:00:00Z',
    },
    effr: {
      source: 'NYFED_API',
      rate_percent: '3.63',
      effective_date: '2026-08-26',
      fetched_at: '2026-08-27T13:00:00Z',
      target_rate_from: '3.50',
      target_rate_to: '3.75',
      revision_indicator: '',
      valid: true,
      reason: 'New York Fed official EFFR',
    },
    quotes: {
      '202609': {
        instrument: '202609',
        bid: '96.3675',
        ask: '96.3700',
        bid_size: '100',
        ask_size: '100',
        last: '96.3700',
        received_at: '2026-08-27T09:00:00Z',
        quality: 'LIVE',
        role: 'TARGET',
        last_price_change_at: '2026-08-27T08:45:00Z',
        last_market_data_event_at: '2026-08-27T09:00:00Z',
        market_data_type: 1,
        subscription_status: 'ACTIVE',
        subscription_generation: 4,
        farm_status: 'CONNECTED',
        analytics_qualified: true,
        pretrade_qualified: true,
        validation_reason: 'current-generation live subscription qualified',
      },
    },
    mapping: {
      verified: true,
      rule_hash_match: true,
      market_count_match: true,
      checked_at: '2026-08-27T09:00:00Z',
      errors: [],
    },
    eligibility: {
      checked: true,
      blocked: false,
      country: 'HK',
      permitted_for_live: true,
      reason: 'opening orders permitted',
    },
    alerts: [
      {
        alert_id: 'resolved-alert',
        severity: 'WARNING',
        code: 'IBKR_2103_USFUTURE',
        message: 'recovered farm interruption',
        flashing: false,
        acknowledged: false,
        resolved: true,
        created_at: '2026-08-27T08:59:00Z',
        resolved_at: '2026-08-27T09:00:00Z',
      },
    ],
    health_messages: [],
    config_version: 'dev-004',
    strategy_version: 'sep-2026-v1.1',
    software_version: 'test',
  } as unknown as EngineSnapshot
}

describe('HealthPanel', () => {
  it('separates farm and subscription health and labels recovered alerts', () => {
    render(<HealthPanel state={stateFixture()} />)

    expect(screen.getByText('US FUTURES FARM')).toBeTruthy()
    expect(screen.getByText('EFFR')).toBeTruthy()
    expect(screen.getByText('SEP EXEC SUB')).toBeTruthy()
    expect(screen.getByText('IBKR_2103_USFUTURE · RESOLVED')).toBeTruthy()
  })
})
