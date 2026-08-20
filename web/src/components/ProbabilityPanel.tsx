import type { EngineSnapshot } from '../types'
import { age, number, pct } from '../format'
import { Panel, Metric } from './Panel'

const monthLabel: Record<string, string> = { '202608': 'AUG 26', '202609': 'SEP 26', '202610': 'OCT 26', '202611': 'NOV 26' }

export function ProbabilityPanel({ state }: { state: EngineSnapshot }) {
  const p = state.probabilities
  return <Panel title="Probability & middle calculation" eyebrow="AUTHORITATIVE BACKEND CALCULATION" className="wide">
    <div className="quote-strip">
      {Object.entries(state.quotes).map(([month, quote]) => <div className="quote-card" key={month}>
        <div><b>{monthLabel[month] ?? month}</b><span className={quote.quality === 'LIVE' ? 'live' : 'warn'}>{quote.quality}</span></div>
        <dl><dt>BID</dt><dd>{number(quote.bid, 4)}</dd><dt>ASK</dt><dd>{number(quote.ask, 4)}</dd><dt>LAST</dt><dd>{number(quote.last, 4)}</dd></dl>
        <small>received {age(quote.received_at)} ago</small>
      </div>)}
    </div>
    <div className="calc-flow">
      <div className="calc-block"><span>1 · IMPLIED MONTHLY EFFR</span>{Object.entries(p.rates).map(([month, rate]) => <div key={month}><code>100 − ZQ {monthLabel[month] ?? month}</code><b>{number(rate, 4)}%</b></div>)}</div>
      <div className="flow-arrow">→</div>
      <div className="calc-block"><span>2 · CALENDAR DECOMPOSITION</span><div><code>Sep start EFFR</code><b>{number(p.september_start_effr, 4)}%</b></div><div><code>Oct start / Sep end</code><b>{number(p.october_start_effr, 4)}%</b></div><div><code>Sep residual</code><b>{number(p.september_residual_bps, 2)} bp</b></div></div>
      <div className="flow-arrow">→</div>
      <div className="calc-block"><span>3 · EXPECTED MEETING MOVE</span><div><code>(end − start) × 100</code><b>{number(p.expected_move_bps, 2)} bp</b></div><div><code>move ÷ 25 bp</code><b>{number(p.expected_steps, 4)} steps</b></div><div><code>floor / remainder</code><b>{p.lower_step_bps ?? '—'} / {pct(p.upper_probability)}</b></div></div>
    </div>
    <div className="prob-summary">
      <div className="distribution"><div><span>{p.lower_step_bps ?? '—'} BP</span><b>{pct(p.lower_probability)}</b><i style={{ width: `${Math.min(100, Math.max(0, Number(p.lower_probability) * 100))}%` }} /></div><div><span>{p.upper_step_bps ?? '—'} BP</span><b>{pct(p.upper_probability)}</b><i style={{ width: `${Math.min(100, Math.max(0, Number(p.upper_probability) * 100))}%` }} /></div></div>
      <Metric label="Executable ZQ probability" value={pct(p.executable_long_probability)} note={p.reason} tone={p.valid ? 'positive' : 'negative'} />
      <Metric label="Snapshot" value={`#${state.snapshot_id}`} note={new Date(state.generated_at).toLocaleTimeString()} />
    </div>
    <div className="comparison-table">
      <div className="subhead"><span>ZQ FEDWATCH DISTRIBUTION VS POLYMARKET YES BOOK</span><small>Mid gap is informational; scenario P&amp;L governs execution.</small></div>
      <table>
        <thead><tr><th>Outcome</th><th>ZQ probability</th><th>Poly bid</th><th>Poly ask</th><th>Poly mid</th><th>ZQ − mid</th><th>Book age</th><th>Mapping</th></tr></thead>
        <tbody>{state.probability_comparisons.map((row) => <tr key={row.code}><td>{row.label}</td><td>{pct(row.zq_probability)}</td><td>{pct(row.polymarket_bid)}</td><td>{pct(row.polymarket_ask)}</td><td>{pct(row.polymarket_mid)}</td><td className={Number(row.midpoint_gap) >= 0 ? 'positive' : 'negative'}>{pct(row.midpoint_gap)}</td><td>{row.book_age_ms === null ? '—' : `${row.book_age_ms}ms`}</td><td className={row.mapping_verified ? 'positive' : 'negative'}>{row.mapping_verified ? 'VERIFIED' : 'BLOCKED'}</td></tr>)}</tbody>
      </table>
    </div>
  </Panel>
}
