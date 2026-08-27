import type { EngineSnapshot } from '../types'
import { age, number, pct } from '../format'
import { Panel, Metric } from './Panel'

const monthLabel: Record<string, string> = {
  '202608': 'AUG 26',
  '202609': 'SEP 26',
  '202610': 'OCT 26',
  '202611': 'NOV 26',
}

export function ProbabilityPanel({ state }: { state: EngineSnapshot }) {
  const p = state.probabilities
  const diagnostic = p.fedwatch
  const gapTone = p.expected_move_gap_bps === null
    ? ''
    : Number(p.expected_move_gap_bps) >= 0 ? 'positive' : 'negative'

  return <Panel title="Direct ZQU6 signal & middle calculation" eyebrow="AUTHORITATIVE BACKEND CALCULATION" className="wide">
    <div className="quote-strip">
      {Object.entries(state.quotes).map(([month, quote]) => <div className={'quote-card ' + (month === p.target_contract_month ? 'primary-quote' : '')} key={month}>
        <div><b>{monthLabel[month] ?? month}</b><span className={quote.quality === 'LIVE' ? 'live' : 'warn'}>{quote.quality}</span></div>
        <dl><dt>BID</dt><dd>{number(quote.bid, 4)}</dd><dt>ASK</dt><dd>{number(quote.ask, 4)}</dd><dt>LAST</dt><dd>{number(quote.last, 4)}</dd></dl>
        <small>{month === p.target_contract_month ? 'PRIMARY SIGNAL' : month === '202608' ? 'PRE-MEETING ANCHOR' : 'DIAGNOSTIC ONLY'} · received {age(quote.received_at)} ago</small>
      </div>)}
    </div>
    <div className="calc-flow">
      <div className="calc-block"><span>1 · SEPTEMBER CONTRACT</span><div><code>ZQU6 bid / ask</code><b>{number(p.target_bid, 4)} / {number(p.target_ask, 4)}</b></div><div><code>ZQU6 midpoint</code><b>{number(p.target_mid, 4)}</b></div><div><code>100 − ZQU6 mid</code><b>{number(p.implied_average_effr_mid, 4)}%</b></div></div>
      <div className="flow-arrow">→</div>
      <div className="calc-block"><span>2 · CALENDAR WEIGHTING</span><div><code>August anchor EFFR</code><b>{number(p.pre_meeting_effr, 4)}%</b></div><div><code>Post-decision weight</code><b>{pct(p.post_decision_weight)}</b></div><div><code>(Sep avg − anchor) ÷ weight</code><b>{number(p.expected_move_bps, 2)} bp</b></div></div>
      <div className="flow-arrow">→</div>
      <div className="calc-block"><span>3 · EXECUTABLE IMPLIED MOVE</span><div><code>Buy ZQ at ask</code><b>{number(p.executable_long_expected_move_bps, 2)} bp</b></div><div><code>Sell ZQ at bid</code><b>{number(p.executable_short_expected_move_bps, 2)} bp</b></div><div><code>Adjacent states</code><b>{p.lower_step_bps ?? '—'} / {p.upper_step_bps ?? '—'} bp</b></div></div>
    </div>
    <div className="prob-summary">
      <div className="distribution">
        <div><span>{p.lower_step_bps ?? '—'} BP DIRECT</span><b>{pct(p.lower_probability)}</b><i style={{ width: String(Math.min(100, Math.max(0, Number(p.lower_probability) * 100))) + '%' }} /></div>
        <div><span>{p.upper_step_bps ?? '—'} BP DIRECT</span><b>{pct(p.upper_probability)}</b><i style={{ width: String(Math.min(100, Math.max(0, Number(p.upper_probability) * 100))) + '%' }} /></div>
      </div>
      <Metric label="Polymarket expected move" value={number(p.polymarket_expected_move_bps, 2) + ' bp'} note={'Normalized from five mids; raw sum ' + pct(p.polymarket_probability_sum)} />
      <Metric label="ZQ − Polymarket move" value={number(p.expected_move_gap_bps, 2) + ' bp'} note={p.reason} tone={gapTone} />
    </div>
    <div className="comparison-table">
      <div className="subhead"><span>DIRECT ADJACENT-STATE ZQ MODEL VS POLYMARKET YES BOOK</span><small>Probability gaps are informational; scenario P&amp;L governs execution.</small></div>
      <table>
        <thead><tr><th>Outcome</th><th>Direct ZQ model</th><th>Poly bid</th><th>Poly ask</th><th>Poly mid</th><th>ZQ − mid</th><th>Last change</th><th>WS book</th><th>Mapping</th></tr></thead>
        <tbody>{state.probability_comparisons.map((row) => <tr key={row.code}><td>{row.label}</td><td>{pct(row.zq_probability)}</td><td>{pct(row.polymarket_bid)}</td><td>{pct(row.polymarket_ask)}</td><td>{pct(row.polymarket_mid)}</td><td className={row.midpoint_gap !== null && Number(row.midpoint_gap) >= 0 ? 'positive' : 'negative'}>{pct(row.midpoint_gap)}</td><td>{row.book_age_ms === null ? '—' : String(row.book_age_ms) + 'ms'}</td><td className={row.stream_synchronized ? 'positive' : 'negative'}>{row.stream_synchronized ? 'SYNC' : 'BLOCKED'}</td><td className={row.mapping_verified ? 'positive' : 'negative'}>{row.mapping_verified ? 'VERIFIED' : 'BLOCKED'}</td></tr>)}</tbody>
      </table>
    </div>
    <details className="diagnostic">
      <summary>Secondary FedWatch diagnostic · {diagnostic.valid ? 'AVAILABLE' : 'AWAITING FOUR CONTRACTS'}</summary>
      <div className="diagnostic-grid">
        <Metric label="FedWatch expected move" value={number(diagnostic.expected_move_bps, 2) + ' bp'} note={diagnostic.reason} />
        <Metric label="September residual" value={number(diagnostic.september_residual_bps, 2) + ' bp'} note="Cross-contract model health only" />
        <Metric label="FedWatch adjacent states" value={String(diagnostic.lower_step_bps ?? '—') + ' / ' + String(diagnostic.upper_step_bps ?? '—') + ' bp'} note={pct(diagnostic.lower_probability) + ' / ' + pct(diagnostic.upper_probability)} />
      </div>
    </details>
  </Panel>
}
