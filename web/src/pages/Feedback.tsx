import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { productFeedback as api } from '../api/endpoints'
import { describe, useLoad } from '../api/useLoad'
import type { Brand, ProductFeedback } from '../api/types'
import { Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { BrandTag, Card, CardHeader, Chip, Empty, ErrorState, KV, Loading, Modal, StatusChip } from '../components/ui'
import { relTime } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'

interface FeedbackBundle { brand: Brand; items: ProductFeedback[] }

export default function Feedback() {
  const { id } = useParams(); const navigate = useNavigate(); const { active, selected } = useBrands(); const { notify } = useToast()
  const [status, setStatus] = useState('active'); const [busy, setBusy] = useState(false); const [action, setAction] = useState<'start' | 'resolve' | 'verify' | 'reopen' | null>(null); const [comment, setComment] = useState('')
  const list = useLoad<FeedbackBundle[]>(() => Promise.all(active.map(async (brand) => ({ brand, items: await api.list(brand.slug) }))), [active.map((brand) => brand.slug).join('|')])
  const entries = useMemo(() => (list.data ?? []).flatMap((bundle) => bundle.items.map((item) => ({ brand: bundle.brand, item }))).filter(({ item }) => status === 'all' || (status === 'active' ? ['open', 'in_progress'].includes(item.status) : item.status === status)), [list.data, status])
  const selectedEntry = (list.data ?? []).flatMap((bundle) => bundle.items.map((item) => ({ brand: bundle.brand, item }))).find(({ item }) => item.id === id) ?? entries[0]
  const selectedBrandSlug = selectedEntry?.brand.slug; const selectedItemId = selectedEntry?.item.id
  const detail = useLoad(async () => {
    if (!selectedBrandSlug || !selectedItemId) return null
    const [item, history] = await Promise.all([api.get(selectedBrandSlug, selectedItemId), api.history(selectedBrandSlug, selectedItemId)])
    return { ...item, history }
  }, [selectedBrandSlug, selectedItemId])
  const q = selected ? `?brand=${encodeURIComponent(selected)}` : ''
  useEffect(() => { if (selectedItemId && selectedItemId !== id) navigate(`/feedback/${encodeURIComponent(selectedItemId)}${q}`, { replace: true }) }, [selectedItemId, id, navigate, q])
  const run = async (success: string, operation: () => Promise<unknown>) => { setBusy(true); try { await operation(); notify(success); list.reload(); detail.reload(); setAction(null); setComment('') } catch (error) { notify(describe(error), 'bad') } finally { setBusy(false) } }
  const item = detail.data; const brand = selectedEntry?.brand
  const submitAction = (body: Record<string, unknown>) => {
    if (!action || !item || !brand) return
    const operation = action === 'start'
      ? api.start(brand.slug, item.id, body as { assignee: string; implementation_links?: string[]; implementation_notes?: string })
      : action === 'resolve'
        ? api.resolve(brand.slug, item.id, body as { resolution_evidence: string; implementation_links?: string[]; implementation_notes?: string })
        : action === 'verify'
          ? api.verify(brand.slug, item.id, String(body.evidence))
          : api.reopen(brand.slug, item.id, String(body.reason))
    void run(`${action[0].toUpperCase()}${action.slice(1)} action recorded.`, () => operation)
  }
  return <Shell title="Product feedback" crumb="Build the product from operating evidence">
    <div className="row"><label className="meta">Show <select className="input" aria-label="Feedback status" value={status} onChange={(e) => setStatus(e.target.value)}><option value="active">Active</option><option value="all">All</option><option value="open">Open</option><option value="in_progress">In progress</option><option value="resolved">Resolved</option><option value="verified">Verified</option></select></label><span className="meta">Agent-reported failures and operator decisions share one audited lifecycle.</span></div>
    {list.error && <ErrorState message={list.error} retry={list.reload} />}{list.loading && !list.data && <Loading />}
    <div className="grid feedback-layout">
      <Card><CardHeader title={`${entries.length} feedback items`} />{!entries.length ? <Empty>No feedback matches this view.</Empty> : entries.map((entry) => <button className={`list-item ${entry.item.id === selectedEntry?.item.id ? 'on' : ''}`} key={entry.item.id} onClick={() => navigate(`/feedback/${encodeURIComponent(entry.item.id)}${q}`)}><div className="row"><BrandTag brand={entry.brand} short /><Chip kind={entry.item.severity === 'critical' || entry.item.severity === 'high' ? 'human' : 'neutral'}>{entry.item.severity}</Chip><span className="meta" style={{ marginLeft: 'auto' }}>{relTime(entry.item.updated_at)}</span></div><b>{entry.item.summary}</b><div className="meta">{entry.item.component} · reported by {entry.item.reporter}{entry.item.occurrence_count > 1 ? ` · ${entry.item.occurrence_count} occurrences` : ''}</div></button>)}</Card>
      <Card>{detail.error && <ErrorState message={detail.error} retry={detail.reload} />}{detail.loading && !item && <Loading />}{item && brand && <>
        <CardHeader title={<BrandTag brand={brand} />} sub={item.component} right={<StatusChip status={item.status} />} />
        <div className="card-b stack" style={{ gap: 14 }}><div><h2 style={{ margin: 0 }}>{item.summary}</h2><div className="meta">Reported by <b>{item.reporter}</b> · first seen {relTime(item.first_seen_at)} · last seen {relTime(item.last_seen_at)}</div></div><p style={{ whiteSpace: 'pre-wrap' }}>{item.details}</p>
          <KV rows={[{ k: 'Severity', v: item.severity }, { k: 'Assignee', v: item.assignee || 'unassigned' }, { k: 'Reproduction', v: item.reproduction || 'not recorded' }, { k: 'Expected', v: item.expected_behavior || 'not recorded' }, { k: 'Actual', v: item.actual_behavior || 'not recorded' }, { k: 'Workaround', v: item.workaround || 'none recorded' }]} />
          {!!item.implementation_links.length && <div><b>Implementation links</b>{item.implementation_links.map((link) => <div className="mono meta" key={link}>{link}</div>)}</div>}
          {item.resolution_evidence && <div className="ai-note"><Icon name="check" size={15} /><div><b>Resolution evidence</b><div>{item.resolution_evidence}</div></div></div>}
          <div className="row">{item.status === 'open' && <button className="btn" disabled={busy} onClick={() => setAction('start')}>Start work</button>}{item.status === 'in_progress' && <button className="btn ok" disabled={busy} onClick={() => setAction('resolve')}>Resolve with evidence</button>}{item.status === 'resolved' && <button className="btn ok" disabled={busy} onClick={() => setAction('verify')}>Verify outcome</button>}{item.status !== 'open' && <button className="btn danger" disabled={busy} onClick={() => setAction('reopen')}>Reopen</button>}</div>
        </div>
        <div className="card-b stack" style={{ borderTop: '1px solid var(--line-2)' }}><b>Comments</b>{!item.comments?.length && <div className="meta">No comments yet.</div>}{item.comments?.map((entry) => <div className="feed-item" key={entry.id}><div><b>{entry.actor}</b><div>{entry.body}</div></div><span className="meta" style={{ marginLeft: 'auto' }}>{relTime(entry.created_at)}</span></div>)}<div className="row"><input className="input" aria-label="Comment" value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Add an operating note" /><button className="btn" disabled={busy || !comment.trim()} onClick={() => void run('Comment added from your authenticated session.', () => api.comment(brand.slug, item.id, comment.trim()))}>Comment</button></div></div>
        <div className="card-b stack" style={{ borderTop: '1px solid var(--line-2)' }}><b>History</b>{item.history?.map((event) => <div className="feed-item" key={event.sequence}><Icon name="history" size={14} /><div><b>{event.actor}</b> {event.action.replace(/_/g, ' ')}<div className="meta">{event.from_status ?? 'new'} → {event.to_status ?? event.from_status}</div></div><span className="meta" style={{ marginLeft: 'auto' }}>{relTime(event.at)}</span></div>)}</div>
      </>}</Card>
    </div>
    {action && item && brand && <FeedbackAction action={action} item={item} busy={busy} close={() => setAction(null)} submit={submitAction} />}
  </Shell>
}

function FeedbackAction({ action, item, busy, close, submit }: { action: 'start' | 'resolve' | 'verify' | 'reopen'; item: ProductFeedback; busy: boolean; close: () => void; submit: (body: Record<string, unknown>) => void }) {
  const [assignee, setAssignee] = useState(item.assignee ?? ''); const [evidence, setEvidence] = useState(''); const [notes, setNotes] = useState(item.implementation_notes ?? ''); const [links, setLinks] = useState(item.implementation_links.join('\n'))
  const required = action === 'start' ? assignee : evidence; const label = action === 'start' ? 'Start work' : action === 'resolve' ? 'Resolve' : action === 'verify' ? 'Verify' : 'Reopen'
  const body = action === 'start' ? { assignee: assignee.trim(), implementation_notes: notes.trim(), implementation_links: lines(links) } : action === 'resolve' ? { resolution_evidence: evidence.trim(), implementation_notes: notes.trim(), implementation_links: lines(links) } : action === 'verify' ? { evidence: evidence.trim() } : { reason: evidence.trim() }
  return <Modal title={`${label}: ${item.summary}`} onClose={close} footer={<><button className="btn" onClick={close}>Cancel</button><button className="btn primary" disabled={busy || !required.trim()} onClick={() => submit(body)}>{label}</button></>}>{action === 'start' && <div className="field"><label htmlFor="feedback-assignee">Assignee</label><input id="feedback-assignee" className="input" value={assignee} onChange={(e) => setAssignee(e.target.value)} /></div>}{action !== 'start' && <div className="field"><label htmlFor="feedback-evidence">{action === 'reopen' ? 'Reason' : action === 'verify' ? 'Verification evidence' : 'Resolution evidence'}</label><textarea id="feedback-evidence" className="input" rows={4} value={evidence} onChange={(e) => setEvidence(e.target.value)} /></div>}{['start', 'resolve'].includes(action) && <><div className="field"><label htmlFor="feedback-links">Implementation links (one per line)</label><textarea id="feedback-links" className="input" rows={3} value={links} onChange={(e) => setLinks(e.target.value)} /></div><div className="field"><label htmlFor="feedback-notes">Implementation notes</label><textarea id="feedback-notes" className="input" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} /></div></>}<div className="meta">The action is attributed to your authenticated session.</div></Modal>
}

function lines(value: string) { return value.split('\n').map((line) => line.trim()).filter(Boolean) }
