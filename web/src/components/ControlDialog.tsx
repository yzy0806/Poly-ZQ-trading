import { useState } from 'react'

export function ControlDialog({ action, close, submit, error }: { action: string; close: () => void; submit: (reason: string, secret: string) => void; error: string }) {
  const [reason, setReason] = useState('')
  const [secret, setSecret] = useState('')
  return <div className="modal-backdrop" role="dialog" aria-modal="true"><form className="modal" onSubmit={(event) => { event.preventDefault(); submit(reason, secret) }}><span>MANUAL CONTROL</span><h2>Confirm {action.replaceAll('_', ' ')}</h2><p>This action is audited. It does not authorize automatic liquidation of filled ZQ.</p><label>Audit reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} minLength={5} required autoFocus /></label><label>Confirmation secret<input value={secret} onChange={(event) => setSecret(event.target.value)} type="password" required /></label>{error && <div className="form-error">{error}</div>}<div><button type="button" onClick={close}>Back</button><button className="primary" type="submit">Confirm action</button></div></form></div>
}
