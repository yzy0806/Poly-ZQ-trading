import type { EngineSnapshot } from './types'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function parseError(response: Response): Promise<never> {
  let message = `${response.status} ${response.statusText}`
  try {
    const payload = await response.json() as { detail?: string }
    if (payload.detail) message = payload.detail
  } catch { /* response was not JSON */ }
  throw new ApiError(response.status, message)
}

export async function login(username: string, password: string): Promise<void> {
  const response = await fetch('/api/v1/session/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!response.ok) await parseError(response)
}

export async function fetchState(): Promise<EngineSnapshot> {
  const response = await fetch('/api/v1/state', { credentials: 'include' })
  if (!response.ok) await parseError(response)
  return response.json() as Promise<EngineSnapshot>
}

function cookie(name: string): string {
  const entry = document.cookie.split('; ').find((value) => value.startsWith(`${name}=`))
  return entry ? decodeURIComponent(entry.split('=').slice(1).join('=')) : ''
}

export async function control(
  action: string,
  reason: string,
  confirmationSecret: string,
  alertId?: string,
): Promise<void> {
  const response = await fetch('/api/v1/control', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': cookie('zq_arb_csrf') },
    body: JSON.stringify({
      action,
      reason,
      confirmation_secret: confirmationSecret,
      alert_id: alertId,
    }),
  })
  if (!response.ok) await parseError(response)
}

export function stateSocket(onState: (state: EngineSnapshot) => void, onClose: () => void): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(`${protocol}//${window.location.host}/api/v1/ws/state`)
  socket.onmessage = (event) => onState(JSON.parse(event.data) as EngineSnapshot)
  socket.onclose = onClose
  return socket
}
