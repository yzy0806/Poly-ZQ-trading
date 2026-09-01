import type { GateCheck } from '../types'

function displayValue(value: string | null, unit: string | null) {
  if (value === null || value === '') return '—'
  return unit ? `${value} ${unit}` : value
}

export function GateTable({ checks, empty = 'No failed checks.' }: { checks: GateCheck[]; empty?: string }) {
  if (!checks.length) return <div className="gate-empty">{empty}</div>
  return <div className="gate-table-wrap"><table className="gate-table">
    <thead><tr><th>Qualification</th><th>Actual</th><th>Rule</th><th>Status</th><th>Detailed reason</th></tr></thead>
    <tbody>{checks.map((check) => <tr key={check.code} className={`gate-${check.status.toLowerCase()}`}>
      <td><b>{check.label}</b><small>{check.category} · {check.code}</small></td>
      <td>{displayValue(check.actual_value, check.unit)}</td>
      <td>{check.operator ?? '—'} {displayValue(check.required_value, check.unit)}</td>
      <td><strong>{check.status}</strong></td>
      <td>{check.detail}</td>
    </tr>)}</tbody>
  </table></div>
}
