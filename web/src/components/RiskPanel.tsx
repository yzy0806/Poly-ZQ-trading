import type { AccountMetrics } from '../types'
import { pct, signedUsd, tone, usd } from '../format'
import { Panel, Metric } from './Panel'

export function RiskPanel({ account }: { account: AccountMetrics }) {
  return <Panel title="Margin & P&L" eyebrow="IBKR ACCOUNT / STRATEGY ALLOCATION">
    <div className="metric-grid"><Metric label="Net liquidation" value={usd(account.net_liquidation)} /><Metric label="Cash value" value={usd(account.total_cash_value)} /><Metric label="Initial margin" value={usd(account.init_margin)} /><Metric label="Maintenance margin" value={usd(account.maintenance_margin)} /><Metric label="Full excess liquidity" value={usd(account.full_excess_liquidity)} /><Metric label="Margin cushion" value={pct(account.cushion)} tone={Number(account.cushion) >= .5 ? 'positive' : 'negative'} /><Metric label="Daily P&L" value={signedUsd(account.daily_pnl)} tone={tone(account.daily_pnl)} /><Metric label="Unrealized P&L" value={signedUsd(account.unrealized_pnl)} tone={tone(account.unrealized_pnl)} /></div>
  </Panel>
}
