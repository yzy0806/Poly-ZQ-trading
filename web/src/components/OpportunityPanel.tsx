import { CheckCircle2, XCircle } from 'lucide-react'
import type { Opportunity } from '../types'
import { number, signedUsd, tone, usd } from '../format'
import { Panel, Metric } from './Panel'
import { Pill } from './Status'

function OpportunityCard({ opportunity }: { opportunity: Opportunity }) {
  return <article className="opportunity">
    <div className="opportunity-head"><div><Pill tone={opportunity.direction === 'LONG' ? 'blue' : 'violet'}>{opportunity.direction} ZQ</Pill><h3>{opportunity.zq_side} 10 @ {number(opportunity.zq_price, 4)}</h3></div>{opportunity.tradeable ? <CheckCircle2 className="positive" /> : <XCircle className="negative" />}</div>
    <div className="hedge-legs">{Object.entries(opportunity.token_requirements).map(([token, shares]) => <div key={token}><span>{token} {opportunity.direction === 'LONG' ? 'YES' : 'NO'}</span><b>{number(shares, 2)} shares</b><small>post {number(opportunity.token_prices[token], 4)} · emergency VWAP {number(opportunity.emergency_token_prices[token], 4)}</small></div>)}</div>
    <table><thead><tr><th>Scenario</th><th>ZQ P&amp;L</th><th>Passive Poly</th><th>Costs</th><th>Passive net</th><th>Emergency net</th></tr></thead><tbody>{opportunity.scenarios.map((row, index) => { const emergency = opportunity.emergency_scenarios[index]; return <tr key={row.move_bps}><td>{row.move_bps > 0 ? '+' : ''}{row.move_bps} bp</td><td className={tone(row.futures_pnl)}>{signedUsd(row.futures_pnl)}</td><td className={tone(row.polymarket_pnl)}>{signedUsd(row.polymarket_pnl)}</td><td>{usd(Number(row.costs) + Number(row.reserves))}</td><td className={tone(row.net_pnl)}><b>{signedUsd(row.net_pnl)}</b></td><td className={tone(emergency?.net_pnl ?? null)}><b>{signedUsd(emergency?.net_pnl ?? null)}</b></td></tr> })}</tbody></table>
    <div className="opportunity-summary"><Metric label="Passive-post minimum" value={signedUsd(opportunity.passive_minimum_net_profit)} tone={tone(opportunity.passive_minimum_net_profit)} /><Metric label="Emergency-cap minimum" value={signedUsd(opportunity.emergency_minimum_net_profit)} tone={tone(opportunity.emergency_minimum_net_profit)} /><Metric label="Conservative minimum" value={signedUsd(opportunity.minimum_net_profit)} tone={tone(opportunity.minimum_net_profit)} /><Metric label="Committed capital" value={usd(opportunity.committed_capital)} /><Metric label="Return on capital" value={`${number(Number(opportunity.return_on_capital_bps) / 100, 2)}%`} /></div>
    <div className="gates"><b>{opportunity.tradeable ? 'ALL GATES PASSED' : `${opportunity.gate_reasons.length} BLOCKING GATES`}</b>{opportunity.gate_reasons.slice(0, 4).map((reason) => <span key={reason}>× {reason}</span>)}</div>
  </article>
}

export function OpportunityPanel({ opportunities }: { opportunities: Opportunity[] }) {
  return <Panel title="Profit waterfall" eyebrow="10-CONTRACT COVERED-STATE MATRIX" className="wide"><div className="opportunity-grid">{opportunities.length ? opportunities.map((item) => <OpportunityCard opportunity={item} key={item.direction} />) : <div className="empty">Waiting for synchronized executable quotes and full hedge depth.</div>}</div></Panel>
}
