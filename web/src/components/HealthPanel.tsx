import type { EngineSnapshot } from '../types'
import { age } from '../format'
import { Panel } from './Panel'
import { Status } from './Status'

export function HealthPanel({ state }: { state: EngineSnapshot }) {
  const farms = state.ibkr_farms ?? {}
  const futuresFarm = farms.US_FUTURES
  const hmdsFarm = farms.HMDS
  const secdefFarm = farms.SECDEF
  const anchorQuote = Object.values(state.quotes).find((quote) => quote.role === 'ANCHOR')
  const targetQuote = Object.values(state.quotes).find((quote) => quote.role === 'TARGET')
  return <Panel title="Audit & health" eyebrow="FAIL-CLOSED OPERATING STATUS" className="wide">
    <div className="health-list"><Status label="IBKR SOCKET" value={state.ibkr.status} good={state.ibkr.status === 'CONNECTED'} /><small>{state.ibkr.message} · {age(state.ibkr.last_message_at)}</small><Status label="US FUTURES FARM" value={futuresFarm?.status ?? 'UNKNOWN'} good={futuresFarm?.status === 'CONNECTED'} /><small>{futuresFarm?.message ?? 'not observed'} · {age(futuresFarm?.last_changed_at ?? null)}</small><Status label="AUG ANCHOR SUB" value={anchorQuote?.subscription_status ?? 'PENDING'} good={anchorQuote?.analytics_qualified ?? false} /><small>{anchorQuote?.validation_reason ?? 'awaiting quote'}</small><Status label="SEP EXEC SUB" value={targetQuote?.subscription_status ?? 'PENDING'} good={targetQuote?.pretrade_qualified ?? false} /><small>{targetQuote?.validation_reason ?? 'awaiting quote'}</small><Status label="HMDS" value={hmdsFarm?.status ?? 'UNKNOWN'} good={hmdsFarm?.status !== 'DISCONNECTED'} /><small>{hmdsFarm?.message ?? 'not observed'} · {age(hmdsFarm?.last_changed_at ?? null)}</small><Status label="SECDEF" value={secdefFarm?.status ?? 'UNKNOWN'} good={secdefFarm?.status !== 'DISCONNECTED'} /><small>{secdefFarm?.message ?? 'not observed'} · {age(secdefFarm?.last_changed_at ?? null)}</small><Status label="POLYMARKET DATA" value={state.polymarket.status} good={state.polymarket.status === 'CONNECTED'} /><small>{state.polymarket.message} · {age(state.polymarket.last_message_at)}</small><Status label="MARKET MAPPING" value={state.mapping.verified ? 'VERIFIED' : 'FAILED'} good={state.mapping.verified} /><small>rule hash {state.mapping.rule_hash_match ? 'match' : 'not verified'} · market count {state.mapping.market_count_match ? 'match' : 'mismatch'}</small><Status label="ELIGIBILITY" value={state.eligibility.blocked === false ? state.eligibility.country ?? 'CLEAR' : 'BLOCKED / UNKNOWN'} good={state.eligibility.blocked === false} /><small>{state.eligibility.reason}</small></div>
    <div className="version"><span>CONFIG {state.config_version}</span><span>STRATEGY {state.strategy_version}</span><span>SOFTWARE {state.software_version}</span></div>
    {state.health_messages.length > 0 && <div className="health-messages">{state.health_messages.map((message) => <span key={message}>{message}</span>)}</div>}
    {state.alerts.some((alert) => alert.severity !== 'CRITICAL') && <div className="venue-alerts">{state.alerts.filter((alert) => alert.severity !== 'CRITICAL').map((alert) => <div className={alert.resolved ? 'resolved' : 'current'} key={alert.alert_id}><b>{alert.code} · {alert.resolved ? 'RESOLVED' : 'CURRENT'}</b><span>{alert.message}</span></div>)}</div>}
  </Panel>
}
