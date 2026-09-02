import type { PortfolioView } from '../types'
import { age, number, signedUsd, tone, usd } from '../format'
import { Metric, Panel } from './Panel'
import { Pill } from './Status'

export function PortfolioPanel({ portfolio }: { portfolio: PortfolioView }) {
  if (!portfolio) return <Panel title="Current cross-venue portfolio" eyebrow="RESTART BACKEND TO LOAD PORTFOLIO STATE" className="wide">
    <div className="empty">Portfolio state is not available from the running backend.</div>
  </Panel>
  return <Panel title="Current cross-venue portfolio" eyebrow="STRATEGY-ATTRIBUTED POSITIONS / EXECUTABLE-BID MARKS" className="wide">
    <div className="portfolio-summary">
      <Metric label="Combined unrealized P&L" value={signedUsd(portfolio.combined_unrealized_pnl, 2)} tone={tone(portfolio.combined_unrealized_pnl)} note="Gross mark-to-market before commissions and fees" />
      <Metric label="ZQ unrealized P&L" value={signedUsd(portfolio.zq_unrealized_pnl, 2)} tone={tone(portfolio.zq_unrealized_pnl)} note="Contracts × $4,167 × price change" />
      <Metric label="Polymarket unrealized P&L" value={signedUsd(portfolio.polymarket_unrealized_pnl, 2)} tone={tone(portfolio.polymarket_unrealized_pnl)} note="Shares × (best bid − average entry)" />
      <Metric label="Valuation status" value={portfolio.valuation_complete ? 'COMPLETE' : 'PARTIAL'} tone={portfolio.valuation_complete ? 'positive' : 'negative'} note={`${portfolio.valuation_reason} · ${age(portfolio.valued_at)}`} />
    </div>
    {portfolio.positions.length ? <div className="portfolio-table-wrap"><table className="portfolio-table">
      <thead><tr><th>Venue / instrument</th><th>Strategy qty</th><th>Venue qty</th><th>Average entry</th><th>Executable mark</th><th>Cost basis</th><th>Liquidation value</th><th>Unrealized P&amp;L</th><th>Ledger check</th></tr></thead>
      <tbody>{portfolio.positions.map((position) => <tr key={`${position.venue}:${position.instrument}`}>
        <td><div className="portfolio-instrument"><Pill tone={position.venue === 'IBKR' ? 'blue' : 'amber'}>{position.venue}</Pill><b>{position.label}</b><small>{position.simulated ? 'SIMULATED POSITION' : position.instrument.length > 20 ? `${position.instrument.slice(0, 16)}…` : position.instrument}</small></div></td>
        <td>{number(position.strategy_quantity, 2)}</td>
        <td>{number(position.venue_quantity, 2)}</td>
        <td>{number(position.average_entry_price, position.venue === 'IBKR' ? 5 : 4)}</td>
        <td><b>{number(position.mark_price, position.venue === 'IBKR' ? 5 : 4)}</b><small>{position.mark_source} · {age(position.mark_updated_at)}</small></td>
        <td>{position.venue === 'IBKR' ? 'Variation margin' : usd(position.cost_basis, 2)}</td>
        <td>{position.venue === 'IBKR' ? '—' : usd(position.market_value, 2)}</td>
        <td className={tone(position.unrealized_pnl)}><b>{signedUsd(position.unrealized_pnl, 2)}</b></td>
        <td>{position.reconciled === null ? <Pill tone="neutral">PENDING</Pill> : position.reconciled ? <Pill tone="green">MATCH</Pill> : <Pill tone="red">MISMATCH</Pill>}</td>
      </tr>)}</tbody>
    </table></div> : <div className="empty">No strategy positions have been filled.</div>}
    <div className="portfolio-footnote">P&amp;L uses durable strategy executions and conservative executable bids. “Venue qty” is the independently reported IBKR position, or the simulated Polymarket ledger while paper hedge simulation is enabled. A mismatch is shown explicitly and is never folded silently into strategy P&amp;L.</div>
  </Panel>
}
