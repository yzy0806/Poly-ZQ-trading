import type { AlertView } from '../types'

const unhedgedAlertCodes = new Set([
  'HEDGE_REPRICE_EXHAUSTED',
  'HEDGE_ROUTING_FAILED',
  'POLYMARKET_OUTBOX_RECOVERY_FAILED',
  'POLYMARKET_TRADE_FAILED',
])

export function EmergencyBanner({ alert, acknowledge }: { alert: AlertView; acknowledge: () => void }) {
  const headline = unhedgedAlertCodes.has(alert.code)
    ? 'UNHEDGED ZQ — MANUAL ACTION REQUIRED'
    : 'CRITICAL SYSTEM ALERT — MANUAL ACTION REQUIRED'

  return <div className={`emergency ${alert.flashing ? 'flashing' : ''}`}><strong>{headline}</strong><span>{alert.code}: {alert.message}</span><button onClick={acknowledge}>Acknowledge</button></div>
}
