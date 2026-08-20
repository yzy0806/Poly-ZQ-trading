import { OctagonAlert, Pause, Power, ShieldAlert } from 'lucide-react'
import type { EngineSnapshot } from '../types'
import { age } from '../format'
import { Status, Pill } from './Status'

function clock(zone: string): string {
  return new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date())
}

export function Header({ state, onControl }: { state: EngineSnapshot; onControl: (action: string) => void }) {
  const quoteAge = Math.max(0, ...Object.values(state.quotes).map((quote) => Date.now() - new Date(quote.received_at).getTime()))
  return <header className="topbar">
    <div className="brand"><div className="mark">ZQ</div><div><h1>Cross-Venue Arbitrage</h1><span>FOMC September 2026 · control terminal</span></div></div>
    <div className="top-status">
      <Pill tone={state.run_mode === 'READ_ONLY' ? 'amber' : 'blue'}>{state.run_mode}</Pill>
      <Status label="IBKR" value={state.ibkr.status} good={state.ibkr.status === 'CONNECTED'} />
      <Status label="POLY" value={state.polymarket.status} good={state.polymarket.status === 'CONNECTED'} />
      <Status label="RULES" value={state.mapping.verified ? 'VERIFIED' : 'BLOCKED'} good={state.mapping.verified} />
      <div className="clock"><span>UTC {clock('UTC')}</span><span>NY {clock('America/New_York')}</span><span>TW {clock('Asia/Taipei')}</span></div>
      <div className="age"><span>MAX AGE</span><b>{quoteAge ? age(new Date(Date.now() - quoteAge).toISOString()) : '—'}</b></div>
    </div>
    <div className="controls">
      <button onClick={() => onControl('ARM')} disabled={state.run_mode === 'READ_ONLY' || state.armed}><Power size={15} />Arm</button>
      <button onClick={() => onControl('PAUSE_NEW_TRADES')}><Pause size={15} />Pause</button>
      <button onClick={() => onControl('CANCEL_UNFILLED')}><OctagonAlert size={15} />Cancel</button>
      <button className="danger" onClick={() => onControl('EMERGENCY_HALT')}><ShieldAlert size={15} />Halt</button>
    </div>
  </header>
}
