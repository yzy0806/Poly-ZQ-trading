import { CheckCircle2, XCircle } from 'lucide-react'
import type { Opportunity, ScenarioPnl } from '../types'
import { number, signedUsd, tone, usd } from '../format'
import { GateTable } from './GateTable'
import { Panel, Metric } from './Panel'
import { Pill } from './Status'

function ScenarioPath({ label, row }: { label: string; row: ScenarioPnl }) {
  return <div className="scenario-path">
    <b>{label}</b>
    <div><span>INC25 YES</span><code>{number(row.inc25_shares, 2)} × ({number(row.inc25_payout, 2)} payout − {number(row.inc25_entry_price, 4)} paid)</code><strong className={tone(row.inc25_pnl)}>{signedUsd(row.inc25_pnl, 2)}</strong></div>
    <div><span>INC50PLUS YES</span><code>{number(row.inc50plus_shares, 2)} × ({number(row.inc50plus_payout, 2)} payout − {number(row.inc50plus_entry_price, 4)} paid)</code><strong className={tone(row.inc50plus_pnl)}>{signedUsd(row.inc50plus_pnl, 2)}</strong></div>
    <div><span>Polymarket P&amp;L</span><code>{signedUsd(row.inc25_pnl, 2)} + {signedUsd(row.inc50plus_pnl, 2)}</code><strong className={tone(row.polymarket_pnl)}>{signedUsd(row.polymarket_pnl, 2)}</strong></div>
    <div><span>Net P&amp;L</span><code>{row.costs === null ? 'Unavailable until current fee parameters load' : `${signedUsd(row.futures_pnl, 2)} + ${signedUsd(row.polymarket_pnl, 2)} − ${usd(row.costs, 2)} costs`}</code><strong className={tone(row.net_pnl)}>{signedUsd(row.net_pnl, 2)}</strong></div>
  </div>
}

function ScenarioCalculation({ passive, emergency }: { passive: ScenarioPnl; emergency: ScenarioPnl }) {
  return <article className="scenario-calculation">
    <header><b>{passive.move_bps > 0 ? '+' : ''}{passive.move_bps} bp scenario</b><span>Settlement {number(passive.settlement_price, 5)}</span></header>
    <div className="futures-formula"><span>ZQ P&amp;L</span><code>{passive.contracts} × ${number(passive.futures_point_value, 0)} × ({number(passive.settlement_price, 5)} − {number(passive.zq_entry_price, 5)})</code><strong className={tone(passive.futures_pnl)}>{signedUsd(passive.futures_pnl, 2)}</strong></div>
    <div className="scenario-path-grid">
      <ScenarioPath label="LOWEST-ASK LIMIT HEDGE" row={passive} />
      <ScenarioPath label="EMERGENCY-CAP HEDGE" row={emergency} />
    </div>
  </article>
}

function CalculationAudit({ opportunity }: { opportunity: Opportunity }) {
  const calculation = opportunity.calculation
  if (!calculation || !opportunity.scenarios.length) return null
  const costs = calculation.costs
  return <details className="calculation-audit" open>
    <summary>How every profit number is calculated</summary>
    <div className="calculation-foundation">
      <div><span>INC25 shares</span><code>{opportunity.contracts} contracts × {number(calculation.inc25_shares_per_contract, 2)}</code><strong>{number(opportunity.token_requirements.INC25, 2)}</strong></div>
      <div><span>INC50PLUS shares</span><code>{opportunity.contracts} contracts × {number(calculation.inc50plus_shares_per_contract, 2)}</code><strong>{number(opportunity.token_requirements.INC50PLUS, 2)}</strong></div>
      <div><span>Emergency hedge cash</span><code>{usd(calculation.inc25_emergency_hedge_cash, 2)} + {usd(calculation.inc50plus_emergency_hedge_cash, 2)}</code><strong>{usd(calculation.emergency_hedge_cash, 2)}</strong></div>
      <div><span>Committed capital</span><code>{calculation.incremental_initial_margin === null ? 'Awaiting current IBKR margin preview' : `${usd(calculation.emergency_hedge_cash, 2)} hedge + ${usd(calculation.incremental_initial_margin, 2)} margin + ${usd(calculation.emergency_cash_reserve, 2)} reserve`}</code><strong>{usd(calculation.committed_capital, 2)}</strong></div>
      <div><span>Conservative minimum</span><code>min({signedUsd(opportunity.passive_minimum_net_profit, 2)} lowest-ask, {signedUsd(opportunity.emergency_minimum_net_profit, 2)} emergency)</code><strong className={tone(opportunity.minimum_net_profit)}>{signedUsd(opportunity.minimum_net_profit, 2)}</strong></div>
      <div><span>Return on capital</span><code>{calculation.committed_capital === null ? 'Awaiting current committed capital' : `${signedUsd(opportunity.minimum_net_profit, 2)} ÷ ${usd(calculation.committed_capital, 2)} × 100`}</code><strong>{opportunity.return_on_capital_bps === null ? '—' : `${number(Number(opportunity.return_on_capital_bps) / 100, 2)}%`}</strong></div>
    </div>
    <div className="cost-formulas">
      <div><span>Explicit costs</span><code>IBKR {usd(costs.ibkr_commission, 2)} + Poly fees {costs.polymarket_fees === null ? 'unavailable' : usd(costs.polymarket_fees, 2)}</code><strong>{usd(costs.explicit_costs, 2)}</strong></div>
    </div>
    <div className="scenario-calculation-grid">
      {opportunity.scenarios.map((passive, index) => {
        const emergency = opportunity.emergency_scenarios[index]
        return emergency ? <ScenarioCalculation key={passive.move_bps} passive={passive} emergency={emergency} /> : null
      })}
    </div>
  </details>
}

function OpportunityCard({ opportunity }: { opportunity: Opportunity }) {
  const checks = opportunity.gate_checks ?? []
  const blockingChecks = checks.filter((check) => check.blocking && check.status !== 'PASSED')
  const passedChecks = checks.filter((check) => check.status === 'PASSED')
  return <article className="opportunity long-only-opportunity">
    <div className="opportunity-head"><div><Pill tone="blue">LONG ZQ</Pill><h3>BUY {opportunity.contracts} @ {number(opportunity.zq_price, 4)}</h3></div>{opportunity.tradeable ? <CheckCircle2 className="positive" /> : <XCircle className="negative" />}</div>
    <div className="hedge-legs">{Object.entries(opportunity.token_requirements).map(([token, shares]) => <div key={token}><span>{token} YES</span><b>{number(shares, 2)} shares</b><small>lowest ask {number(opportunity.token_prices[token], 4)} · emergency VWAP {number(opportunity.emergency_token_prices[token], 4)}</small></div>)}</div>
    {!!opportunity.hedge_depth.length && <div className="hedge-depth-grid">{opportunity.hedge_depth.map((depth) => <div key={depth.leg_code} className={depth.sufficient && depth.marketable_limit_price !== null ? 'depth-pass' : 'depth-fail'}><b>{depth.leg_code}</b><span>Required <strong>{number(depth.required_shares, 2)}</strong></span><span>At lowest ask <strong>{number(depth.best_ask_shares, 2)}</strong></span><span>Shortfall <strong>{number(depth.shortfall_shares, 2)}</strong></span><span>BUY limit <strong>{number(depth.marketable_limit_price, 4)}</strong></span></div>)}</div>}
    <table><thead><tr><th>Scenario</th><th>ZQ P&amp;L</th><th>Passive Poly</th><th>Costs</th><th>Passive net</th><th>Emergency net</th></tr></thead><tbody>{opportunity.scenarios.map((row, index) => { const emergency = opportunity.emergency_scenarios[index]; return <tr key={row.move_bps}><td>{row.move_bps > 0 ? '+' : ''}{row.move_bps} bp</td><td className={tone(row.futures_pnl)}>{signedUsd(row.futures_pnl)}</td><td className={tone(row.polymarket_pnl)}>{signedUsd(row.polymarket_pnl)}</td><td>{usd(row.costs)}</td><td className={tone(row.net_pnl)}><b>{signedUsd(row.net_pnl)}</b></td><td className={tone(emergency?.net_pnl ?? null)}><b>{signedUsd(emergency?.net_pnl ?? null)}</b></td></tr> })}</tbody></table>
    <div className="opportunity-summary"><Metric label="Lowest-ask minimum" value={signedUsd(opportunity.passive_minimum_net_profit)} tone={tone(opportunity.passive_minimum_net_profit)} /><Metric label="Emergency-cap minimum" value={signedUsd(opportunity.emergency_minimum_net_profit)} tone={tone(opportunity.emergency_minimum_net_profit)} /><Metric label="Conservative minimum" value={signedUsd(opportunity.minimum_net_profit)} tone={tone(opportunity.minimum_net_profit)} /><Metric label="Committed capital" value={usd(opportunity.committed_capital)} /><Metric label="Return on capital" value={opportunity.return_on_capital_bps === null ? '—' : `${number(Number(opportunity.return_on_capital_bps) / 100, 2)}%`} /></div>
    <CalculationAudit opportunity={opportunity} />
    <section className="gate-report" aria-label="Complete opportunity qualification report">
      <div className="gate-report-title"><b>{opportunity.tradeable ? 'ALL BLOCKING GATES PASSED' : `${blockingChecks.length || opportunity.gate_reasons.length} BLOCKING GATES`}</b><span>No blocking gate is hidden.</span></div>
      {checks.length ? <GateTable checks={blockingChecks} empty="All blocking gates passed." /> : <div className="legacy-gates">{opportunity.gate_reasons.map((reason) => <span key={reason}>× {reason}</span>)}</div>}
      {!!passedChecks.length && <details className="passed-gates"><summary>{passedChecks.length} passed opportunity checks</summary><GateTable checks={passedChecks} /></details>}
    </section>
  </article>
}

export function OpportunityPanel({ opportunities }: { opportunities: Opportunity[] }) {
  const longOpportunity = opportunities.find((item) => item.direction === 'LONG')
  return <Panel title="Long ZQ profit waterfall" eyebrow="LONG-ONLY 10-CONTRACT COVERED-STATE MATRIX" className="wide"><div className="opportunity-grid">{longOpportunity ? <OpportunityCard opportunity={longOpportunity} /> : <div className="empty">Waiting for synchronized executable quotes and full YES-hedge depth.</div>}</div></Panel>
}
