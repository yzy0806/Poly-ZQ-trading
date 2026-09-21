import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { EngineSnapshot } from '../types'
import { Header } from './Header'

function snapshot(update: Partial<EngineSnapshot> = {}): EngineSnapshot {
  return {
    run_mode: 'PAPER',
    armed: true,
    paused: false,
    kill_switch: false,
    ibkr: { status: 'CONNECTED' },
    polymarket: { status: 'CONNECTED' },
    mapping: { verified: true },
    quotes: {},
    opportunities: [],
    active_batch: { batch_id: null, state: 'IDLE' },
    ...update,
  } as EngineSnapshot
}

describe('Header operating status', () => {
  afterEach(cleanup)

  it('shows maintenance without hiding operator pause or halt', () => {
    const maintenance = { active: true, reopen_at: '2026-09-14T22:00:00Z', reason: 'Scheduled maintenance' }
    const state = snapshot({ metadata: { maintenance } })
    const { rerender } = render(<Header state={state} onControl={() => undefined} />)
    expect(screen.getByText('ARMED · MAINTENANCE')).toBeTruthy()
    expect(screen.getByText(/Scheduled reopening/)).toBeTruthy()
    rerender(<Header state={{ ...state, paused: true, armed: false }} onControl={() => undefined} />)
    expect(screen.getByText('PAUSED')).toBeTruthy()
    rerender(<Header state={{ ...state, kill_switch: true }} onControl={() => undefined} />)
    expect(screen.getByText('HALTED')).toBeTruthy()
  })

  it('shows an armed system waiting for qualification', () => {
    render(<Header state={snapshot()} onControl={() => undefined} />)

    expect(screen.getByText('ARMED · WAITING')).toBeTruthy()
  })

  it('distinguishes a working batch and an emergency halt', () => {
    const { rerender } = render(
      <Header
        state={snapshot({ active_batch: { batch_id: 'batch-1', state: 'ZQ_SUBMITTED' } as EngineSnapshot['active_batch'] })}
        onControl={() => undefined}
      />,
    )
    expect(screen.getByText('ARMED · WORKING')).toBeTruthy()

    rerender(
      <Header
        state={snapshot({ armed: false, paused: true, kill_switch: true })}
        onControl={() => undefined}
      />,
    )
    expect(screen.getByText('HALTED')).toBeTruthy()
  })
})
