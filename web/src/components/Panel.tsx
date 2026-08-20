export function Panel({ title, eyebrow, children, className = '' }: {
  title: string
  eyebrow?: string
  children: React.ReactNode
  className?: string
}) {
  return <section className={`panel ${className}`}>
    <header className="panel-head"><div>{eyebrow && <span>{eyebrow}</span>}<h2>{title}</h2></div></header>
    {children}
  </section>
}

export function Metric({ label, value, note, tone = '' }: { label: string; value: string; note?: string; tone?: string }) {
  return <div className="metric"><span>{label}</span><strong className={tone}>{value}</strong>{note && <small>{note}</small>}</div>
}
