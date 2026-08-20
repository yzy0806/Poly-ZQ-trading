import type { DecimalValue } from './types'

export function number(value: DecimalValue, digits = 2): string {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toLocaleString(undefined, { maximumFractionDigits: digits }) : '—'
}

export function usd(value: DecimalValue): string {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  return Number.isFinite(parsed)
    ? parsed.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
    : '—'
}

export function pct(value: DecimalValue, sourceIsFraction = true): string {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value) * (sourceIsFraction ? 100 : 1)
  return Number.isFinite(parsed) ? `${parsed.toFixed(2)}%` : '—'
}

export function age(iso: string | null | undefined): string {
  if (!iso) return '—'
  const elapsed = Date.now() - new Date(iso).getTime()
  if (elapsed < 1000) return `${Math.max(0, elapsed)}ms`
  return `${Math.max(0, elapsed / 1000).toFixed(1)}s`
}

export function signedUsd(value: DecimalValue): string {
  const formatted = usd(value)
  if (formatted === '—' || Number(value) === 0) return formatted
  return Number(value) > 0 ? `+${formatted}` : formatted
}

export function tone(value: DecimalValue): string {
  if (value === null) return ''
  return Number(value) >= 0 ? 'positive' : 'negative'
}
