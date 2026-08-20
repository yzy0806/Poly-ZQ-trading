import type { AlertView } from '../types'

export function EmergencyBanner({ alert, acknowledge }: { alert: AlertView; acknowledge: () => void }) {
  return <div className={`emergency ${alert.flashing ? 'flashing' : ''}`}><strong>UNHEDGED ZQ — MANUAL ACTION REQUIRED</strong><span>{alert.code}: {alert.message}</span><button onClick={acknowledge}>Acknowledge</button></div>
}
