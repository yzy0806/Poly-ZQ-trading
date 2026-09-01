import type { EngineSnapshot } from '../types'
import { age, number, pct } from '../format'
import { Panel, Metric } from './Panel'
import { GateTable } from './GateTable'

const monthLabel: Record<string, string> = {
  '202608': 'AUG 26',
  '202609': 'SEP 26',
  '202610': 'OCT 26',
  '202611': 'NOV 26',
}

export function ProbabilityPanel({ state }: { state: EngineSnapshot }) {
  const p = state.probabilities
  const diagnostic = p.fedwatch
  const qualificationChecks = p.qualification_checks ?? []
  const failedChecks = qualificationChecks.filter((check) => check.status === 'FAILED' || check.status === 'UNAVAILABLE')
  const passedChecks = qualificationChecks.filter((check) => check.status === 'PASSED')
  const gapTone = p.expected_move_gap_bps === null
    ? ''
    : Number(p.expected_move_gap_bps) >= 0 ? 'positive' : 'negative'

  return <Panel title="Direct ZQU6 signal & middle calculation" eyebrow="AUTHORITATIVE BACKEND CALCULATION" className="wide">
    <div className={'qualification-banner ' + (p.execution_qualified ? 'qualified' : 'blocked')}><b>{p.qualification_reason ?? (p.execution_qualified ? 'EXECUTION-QUALIFIED immutable snapshot' : 'NOT EXECUTION-QUALIFIED')}</b><span>{failedChecks.length ? `${failedChecks.length} detailed failure${failedChecks.length === 1 ? '' : 's'} shown below` : 'Every execution check passed'}</span></div>
    <section className="qualification-detail" aria-label="Cross-venue qualification detail">
      <div className="subhead"><span>FAILED QUALIFICATIONS — ACTUAL VS REQUIRED</span><small>All values originate from this backend snapshot.</small></div>
      <GateTable checks={failedChecks} empty="No failed cross-venue qualifications." />
      {!!passedChecks.length && <details className="passed-gates"><summary>{passedChecks.length} passed qualification checks</summary><GateTable checks={passedChecks} /></details>}
    </section>
    <div className="quote-strip">
      {Object.entries(state.quotes).map(([month, quote]) => <div className={'quote-card ' + (month === p.target_contract_month ? 'primary-quote' : '')} key={month}>
        <div><b>{monthLabel[month] ?? month}</b><span className={quote.quality === 'LIVE' ? 'live' : 'warn'}>{quote.quality}</span></div>
        <dl><dt>BID</dt><dd>{number(quote.bid, 4)}</dd><dt>ASK</dt><dd>{number(quote.ask, 4)}</dd><dt>LAST</dt><dd>{number(quote.last, 4)}</dd></dl>
        <div className="quote-state"><span>Price changed</span><b>{age(quote.last_price_change_at)}</b><span>Market event</span><b>{age(quote.last_market_data_event_at)} · informational</b><span>BBO size</span><b>{number(quote.bid_size, 0)} / {number(quote.ask_size, 0)}</b><span>Subscription</span><b className={quote.subscription_status === 'ACTIVE' ? 'positive' : 'negative'}>{quote.subscription_status ?? 'PENDING'} · G{quote.subscription_generation ?? '—'}</b><span>Data type</span><b className={quote.market_data_type === 1 ? 'positive' : 'negative'}>{quote.market_data_type === 1 ? 'LIVE (1)' : quote.market_data_type ?? 'UNKNOWN'}</b></div>
        <small>{quote.role === 'TARGET' || month === p.target_contract_month ? 'PRIMARY SIGNAL' : quote.role === 'ANCHOR' || month === '202608' ? 'PRE-MEETING ANCHOR' : 'DIAGNOSTIC ONLY'} · {quote.validation_reason ?? 'awaiting subscription qualification'}</small>
      </div>)}
    </div>
    <div className="calc-flow">
      <div className="calc-block"><span>1 · SEPTEMBER CONTRACT</span><div><code>ZQU6 bid / ask</code><b>{number(p.target_bid, 4)} / {number(p.target_ask, 4)}</b></div><div><code>ZQU6 midpoint</code><b>{number(p.target_mid, 4)}</b></div><div><code>100 − ZQU6 mid</code><b>{number(p.implied_average_effr_mid, 4)}%</b></div></div>
      <div className="flow-arrow">→</div>
      <div className="calc-block"><span>2 · CALENDAR WEIGHTING</span><div><code>August anchor EFFR</code><b>{number(p.pre_meeting_effr, 4)}%</b></div><div><code>Post-decision weight</code><b>{pct(p.post_decision_weight)}</b></div><div><code>(Sep avg − anchor) ÷ weight</code><b>{number(p.expected_move_bps, 2)} bp</b></div></div>
      <div className="flow-arrow">→</div>
      <div className="calc-block"><span>3 · LONG-ONLY EXECUTABLE MOVE</span><div><code>Buy ZQ at ask</code><b>{number(p.executable_buy_expected_move_bps, 2)} bp</b></div><div><code>Bid-side reference</code><b>{number(p.bid_reference_expected_move_bps, 2)} bp</b><small>Non-tradable spread boundary</small></div><div><code>Adjacent states</code><b>{p.lower_step_bps ?? '—'} / {p.upper_step_bps ?? '—'} bp</b></div></div>
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
        <thead><tr><th>Outcome</th><th>Direct ZQ model</th><th>Poly bid</th><th>Bid size</th><th>Poly ask</th><th>Ask size</th><th>Poly mid</th><th>ZQ − mid</th><th>Last change</th><th>WS book</th><th>Mapping</th></tr></thead>
        <tbody>{state.probability_comparisons.map((row) => <tr key={row.code}><td>{row.label}</td><td>{pct(row.zq_probability)}</td><td>{pct(row.polymarket_bid)}</td><td>{number(row.polymarket_bid_size, 2)}</td><td>{pct(row.polymarket_ask)}</td><td>{number(row.polymarket_ask_size, 2)}</td><td>{pct(row.polymarket_mid)}</td><td className={row.midpoint_gap !== null && Number(row.midpoint_gap) >= 0 ? 'positive' : 'negative'}>{pct(row.midpoint_gap)}</td><td>{row.book_age_ms === null ? '—' : String(row.book_age_ms) + 'ms'}</td><td className={row.stream_synchronized ? 'positive' : 'negative'}>{row.stream_synchronized ? 'SYNC' : 'BLOCKED'}</td><td className={row.mapping_verified ? 'positive' : 'negative'}>{row.mapping_verified ? 'VERIFIED' : 'BLOCKED'}</td></tr>)}</tbody>
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
