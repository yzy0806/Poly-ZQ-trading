import type { BatchView } from '../types'
import { number } from '../format'
import { Panel, Metric } from './Panel'
import { Pill } from './Status'

export function ExecutionPanel({ batch }: { batch: BatchView }) {
  return <Panel title="Execution & hedge monitor" eyebrow="ONE ACTIVE 10-CONTRACT BATCH">
    <div className="batch-head"><Pill tone={batch.state === 'IDLE' ? 'neutral' : 'amber'}>{batch.state}</Pill><span>{batch.batch_id ?? 'No active batch'}</span></div>
    <div className="metric-grid"><Metric label="ZQ order ID" value={batch.zq_order_id?.toString() ?? '—'} /><Metric label="Order policy" value="LMT / DAY" /><Metric label="Original quantity" value={batch.original_quantity.toString()} /><Metric label="Filled" value={number(batch.filled_quantity)} /><Metric label="Resting" value={number(batch.remaining_quantity)} /><Metric label="Limit price" value={number(batch.limit_price, 4)} /></div>
    <div className="obligations">{batch.obligations.length ? batch.obligations.map((item) => <div key={item.obligation_id}><span>{item.token_id.slice(0, 12)}…</span><b>{number(item.confirmed_shares)} / {number(item.due_shares)}</b><em>{number(item.deficit_shares)} deficit</em></div>) : <div className="empty compact">No hedge obligations.</div>}</div>
  </Panel>
}
