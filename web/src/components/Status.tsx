import { Circle } from 'lucide-react'

export function Status({ label, value, good }: { label: string; value: string; good: boolean }) {
  return <div className="status"><span>{label}</span><b className={good ? 'ok' : 'bad'}><Circle size={8} fill="currentColor" />{value}</b></div>
}

export function Pill({ children, tone = 'neutral' }: { children: React.ReactNode; tone?: string }) {
  return <span className={`pill ${tone}`}>{children}</span>
}
