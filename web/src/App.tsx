import { useCallback, useEffect, useState } from 'react'
import { ApiError, control, fetchState, login, stateSocket } from './api'
import type { AlertView, EngineSnapshot } from './types'
import { Header } from './components/Header'
import { ProbabilityPanel } from './components/ProbabilityPanel'
import { OpportunityPanel } from './components/OpportunityPanel'
import { ExecutionPanel } from './components/ExecutionPanel'
import { RiskPanel } from './components/RiskPanel'
import { HealthPanel } from './components/HealthPanel'
import { EmergencyBanner } from './components/EmergencyBanner'
import { ControlDialog } from './components/ControlDialog'

function Login({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  return <main className="login-shell"><form className="login" onSubmit={async (event) => { event.preventDefault(); setBusy(true); setError(''); try { await login(username, password); onSuccess() } catch (reason) { setError(reason instanceof Error ? reason.message : 'Login failed') } finally { setBusy(false) } }}><div className="login-mark">ZQ × P</div><span>LOCAL CONTROL TERMINAL</span><h1>Cross-Venue Arbitrage</h1><p>Authenticated access to pricing, risk, execution state, and manual controls.</p><label>Operator<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required autoFocus /></label><label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>{error && <div className="form-error">{error}</div>}<button className="primary" disabled={busy}>{busy ? 'Authenticating…' : 'Enter terminal'}</button><small>Credentials and account identifiers are never displayed or stored in this browser.</small></form></main>
}

export default function App() {
  const [state, setState] = useState<EngineSnapshot | null>(null)
  const [authenticated, setAuthenticated] = useState<boolean | null>(null)
  const [selectedAction, setSelectedAction] = useState<string | null>(null)
  const [controlError, setControlError] = useState('')

  const initialize = useCallback(async () => {
    try { setState(await fetchState()); setAuthenticated(true) }
    catch (error) { if (error instanceof ApiError && error.status === 401) setAuthenticated(false); else setAuthenticated(false) }
  }, [])

  useEffect(() => { void initialize() }, [initialize])
  useEffect(() => {
    if (!authenticated) return
    let socket = stateSocket(setState, () => undefined)
    const reconnect = window.setInterval(() => { if (socket.readyState === WebSocket.CLOSED) socket = stateSocket(setState, () => undefined) }, 3000)
    const fallback = window.setInterval(() => { if (socket.readyState !== WebSocket.OPEN) void fetchState().then(setState).catch(() => undefined) }, 5000)
    return () => { window.clearInterval(reconnect); window.clearInterval(fallback); socket.close() }
  }, [authenticated])

  if (authenticated === null) return <div className="loading">Loading terminal…</div>
  if (!authenticated) return <Login onSuccess={() => { setAuthenticated(true); void initialize() }} />
  if (!state) return <div className="loading">Synchronizing engine state…</div>
  const emergency = state.alerts.find((alert) => alert.severity === 'CRITICAL' && !alert.acknowledged) ?? state.alerts.find((alert) => alert.severity === 'CRITICAL')
  const submitControl = async (reason: string, secret: string, alert?: AlertView) => {
    if (!selectedAction) return
    try { await control(selectedAction, reason, secret, alert?.alert_id); setSelectedAction(null); setControlError('') }
    catch (error) { setControlError(error instanceof Error ? error.message : 'Control request failed') }
  }
  return <div className="app"><Header state={state} onControl={(action) => { setSelectedAction(action); setControlError('') }} />{emergency && <EmergencyBanner alert={emergency} acknowledge={() => { setSelectedAction('ACKNOWLEDGE_ALERT'); setControlError('') }} />}<main className="dashboard"><ProbabilityPanel state={state} /><OpportunityPanel opportunities={state.opportunities} /><ExecutionPanel batch={state.active_batch} /><RiskPanel account={state.account} /><HealthPanel state={state} /></main><footer><span>Snapshot #{state.snapshot_id}</span><span>Generated {new Date(state.generated_at).toLocaleString()}</span><span>All prices and decisions originate from backend snapshot {state.snapshot_id}.</span></footer>{selectedAction && <ControlDialog action={selectedAction} close={() => setSelectedAction(null)} error={controlError} submit={(reason, secret) => void submitControl(reason, secret, selectedAction === 'ACKNOWLEDGE_ALERT' ? emergency : undefined)} />}</div>
}
