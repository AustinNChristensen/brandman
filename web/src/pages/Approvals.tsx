import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { dispatch, newsletters } from '../api/endpoints'
import type { Brand, DispatchItem, NewsletterIssue } from '../api/types'
import { describe, useLoad } from '../api/useLoad'
import { Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { BrandTag, Card, CardHeader, Chip, Empty, ErrorState, KV, Loading, Modal, StatusChip } from '../components/ui'
import { NewsletterBody } from '../components/NewsletterBody'
import { parseDate, relTime, shortDateTime, titleCase } from '../lib/format'
import { newsletterReviewReady, scopeSummary } from '../lib/review'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'
import { metersFrom, useWorkspace } from '../state/useWorkspace'

type Entry =
  | { kind: 'dispatch'; id: string; brand: Brand; item: DispatchItem; updated: string }
  | { kind: 'newsletter'; id: string; brand: Brand; issue: NewsletterIssue; updated: string }

export default function Approvals() {
  const ws = useWorkspace(['awaiting', 'newsletters', 'usage'])
  const { kind, id } = useParams()
  const navigate = useNavigate()
  const { selected } = useBrands()
  const q = selected ? `?brand=${encodeURIComponent(selected)}` : ''

  const entries = useMemo<Entry[]>(() => {
    if (!ws.data) return []
    const out: Entry[] = []
    for (const b of ws.data) {
      for (const item of b.awaiting) out.push({ kind: 'dispatch', id: item.id, brand: b.brand, item, updated: item.updated_at })
      for (const issue of b.newsletters) if (issue.lifecycle === 'fact_checked') out.push({ kind: 'newsletter', id: issue.id, brand: b.brand, issue, updated: issue.updated_at })
    }
    return out.sort((a, b) => (parseDate(a.updated)?.getTime() ?? 0) - (parseDate(b.updated)?.getTime() ?? 0))
  }, [ws.data])

  const current = entries.find((e) => e.kind === kind && e.id === id) ?? entries[0] ?? null
  useEffect(() => {
    if (current && (current.kind !== kind || current.id !== id)) navigate(`/approvals/${current.kind}/${encodeURIComponent(current.id)}${q}`, { replace: true })
  }, [current, kind, id, navigate, q])

  const afterAction = () => { ws.reload(); navigate(`/approvals${q}`) }

  return (
    <Shell title="Approvals" crumb="Queue" meters={metersFrom(ws.data)}>
      {ws.error && <ErrorState message={ws.error} retry={ws.reload} />}
      <div className="grid" style={{ gridTemplateColumns: '340px minmax(0, 1fr)', gap: 16, alignItems: 'start' }}>
        <Card>
          <CardHeader title={`${entries.length} waiting`} sub="· oldest first" right={<button className="btn sm ghost" onClick={ws.reload}><Icon name="refresh" size={14} />Refresh</button>} />
          {ws.loading && !ws.data && <Loading />}
          {ws.data && entries.length === 0 && <Empty>Nothing awaiting approval{selected ? ' for this brand' : ''}.</Empty>}
          {entries.map((e) => (
            <QueueRow key={`${e.kind}:${e.id}`} entry={e} on={current === e} to={`/approvals/${e.kind}/${encodeURIComponent(e.id)}${q}`} />
          ))}
        </Card>
        {current?.kind === 'dispatch' && <DispatchReview key={current.id} item={current.item} brand={current.brand} onDone={afterAction} />}
        {current?.kind === 'newsletter' && <NewsletterReview key={current.id} issue={current.issue} brand={current.brand} onDone={afterAction} />}
        {!current && ws.data && <Card><Empty>Select an item to review.</Empty></Card>}
      </div>
    </Shell>
  )
}

function QueueRow({ entry, on, to }: { entry: Entry; on: boolean; to: string }) {
  const navigate = useNavigate()
  const title = entry.kind === 'dispatch' ? (entry.item.payload.body?.slice(0, 110) || entry.item.id) : (entry.issue.content?.final_title || entry.issue.content?.working_title || 'Untitled newsletter')
  const sub = entry.kind === 'dispatch' ? `${entry.item.connector.toUpperCase()} ${entry.item.approval_scope?.action_type ?? 'post'}` : 'Newsletter · Beehiiv draft'
  return (
    <button className={`list-item ${on ? 'on' : ''}`} onClick={() => navigate(to)}>
      <div className="row" style={{ gap: 8 }}><BrandTag brand={entry.brand} short /><span className="meta">{sub}</span><span className="meta" style={{ marginLeft: 'auto' }}>{relTime(entry.updated)}</span></div>
      <div style={{ fontWeight: 500, lineHeight: 1.3 }}>{title}</div>
      <div>{entry.kind === 'dispatch' ? <Chip kind={entry.item.last_error ? 'human' : 'ok'}>rev {entry.item.revision}{entry.item.last_error ? ' · last attempt failed' : ''}</Chip> : <Chip kind="ok">rev {entry.issue.current_revision} · fact-checked</Chip>}</div>
    </button>
  )
}

/* ------------------------------------------------------------------ */
/* Dispatch item (X post / engagement action)                          */
/* ------------------------------------------------------------------ */

function DispatchReview({ item, brand, onDone }: { item: DispatchItem; brand: Brand; onDone: () => void }) {
  const { notify } = useToast()
  const details = useLoad(async () => {
    const [audit, validation] = await Promise.all([dispatch.audit(item.id), dispatch.validation(item.id)])
    return { audit, validation }
  }, [item.id, item.revision])
  const [modal, setModal] = useState<'approve' | 'reject' | null>(null)
  const [busy, setBusy] = useState(false)
  const scope = item.approval_scope
  const body = item.payload.body ?? ''
  const hasLink = /https?:\/\//i.test(body)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (modal || e.metaKey || e.ctrlKey || e.altKey) return
      const tag = (e.target as HTMLElement | null)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA') return
      if (e.key === 'a' || e.key === 'A') setModal('approve')
      if (e.key === 'r' || e.key === 'R') setModal('reject')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [modal])

  const approve = async () => {
    if (!scope) return
    setBusy(true)
    try {
      await dispatch.approve(item.id, item.revision, scope.review_token)
      notify(`Approved revision ${item.revision}. Delivery still needs an approved handoff — nothing was posted.`)
      onDone()
    } catch (err) { notify(describe(err), 'bad') } finally { setBusy(false); setModal(null) }
  }
  const reject = async () => {
    setBusy(true)
    try {
      await dispatch.reject(item.id, item.revision)
      notify(`Rejected revision ${item.revision}.`)
      onDone()
    } catch (err) { notify(describe(err), 'bad') } finally { setBusy(false); setModal(null) }
  }

  const drafter = details.data?.audit.find((a) => a.action === 'awaiting_approval')?.actor
  return (
    <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 1fr) 360px', gap: 16, alignItems: 'start' }}>
      <Card>
        <CardHeader title={<BrandTag brand={brand} />} sub={<span className="nowrap">{item.connector.toUpperCase()} {scope?.action_type ?? 'post'} · revision {item.revision}{item.canonical_post_id ? <> · <span className="mono">{item.canonical_post_id.slice(0, 8)}</span></> : null}</span>}
          right={<StatusChip status={item.status} />} />
        <div className="card-b stack" style={{ gap: 14, padding: '18px 20px' }}>
          <div className="row" style={{ alignItems: 'flex-start' }}>
            <div style={{ width: 36, height: 36, borderRadius: '50%', background: 'var(--line-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11, fontWeight: 700, flex: '0 0 36px' }}>{brand.name.split(/\s+/).map((w) => w[0]).join('').slice(0, 3).toUpperCase()}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="row" style={{ gap: 6, fontSize: 13 }}><b>{brand.name}</b><span className="meta">{scope?.destination ?? item.connector}</span></div>
              <div className="xpost" style={{ marginTop: 6 }}>{body || <span className="meta">No body in payload.</span>}</div>
            </div>
          </div>
          {scope?.intended_schedule && <div className="meta row"><Icon name="clock" size={14} />Intended schedule: {shortDateTime(scope.intended_schedule)}</div>}
        </div>
        <div style={{ padding: '10px 16px', borderTop: '1px solid var(--line-2)' }} className="stack">
          <div className="meta">Approving records an exact-revision decision. It does not post: delivery still requires an approved handoff and a human confirmation at the provider boundary.</div>
          <div className="row">
            <button className="btn ok" disabled={!scope || busy} onClick={() => setModal('approve')}><Icon name="check" size={15} />Approve revision {item.revision} <span className="kbd" style={{ color: '#fff', borderColor: 'rgba(255,255,255,.4)', background: 'transparent' }}>A</span></button>
            <button className="btn" disabled={busy} onClick={() => setModal('reject')}>Reject revision <span className="kbd">R</span></button>
          </div>
          <div className="meta">Edits happen through the API or MCP and create a new revision, which invalidates any approval.</div>
        </div>
      </Card>
      <div className="stack" style={{ gap: 12 }}>
        <Card>
          <CardHeader icon="spark" iconColor="var(--accent)" title="How it got here" />
          <div className="card-b stack" style={{ gap: 8, color: 'var(--muted)' }}>
            {details.loading && <Loading />}
            {details.error && <span className="meta">{details.error}</span>}
            {details.data && (
              <>
                <div>Submitted for review by <b style={{ color: 'var(--text)' }}>{drafter ?? 'unknown'}</b>{item.canonical_post_id ? ' from a canonical campaign post.' : '.'}</div>
                <div className="stack" style={{ gap: 4 }}>
                  {details.data.audit.slice(-6).map((a) => (
                    <div key={`${a.at}:${a.action}`} className="row" style={{ fontSize: 12 }}><span className="meta nowrap">{relTime(a.at)}</span><span><b>{a.actor}</b> {titleCase(a.action)}{a.detail ? ` — ${a.detail}` : ''}</span></div>
                  ))}
                </div>
              </>
            )}
          </div>
        </Card>
        <Card>
          <CardHeader title="Checks" right={details.data ? <Chip kind={details.data.validation.valid ? 'ok' : 'bad'}>{details.data.validation.valid ? 'valid' : 'invalid'}</Chip> : undefined} />
          <div className="card-b stack" style={{ gap: 7 }}>
            {details.data && (
              <>
                <div className="check-row"><Icon name={details.data.validation.valid ? 'check' : 'alert'} size={14} style={{ color: details.data.validation.valid ? 'var(--ok)' : 'var(--bad)' }} /><span>Connector validation{typeof details.data.validation.effective_length === 'number' ? ` · ${details.data.validation.effective_length} chars` : ''}</span></div>
                {details.data.validation.errors.map((e) => <div key={e} className="check-row"><Icon name="alert" size={14} style={{ color: 'var(--bad)' }} /><span>{e}</span></div>)}
              </>
            )}
            <div className="check-row"><Icon name={hasLink ? 'alert' : 'check'} size={14} style={{ color: hasLink ? 'var(--human)' : 'var(--ok)' }} /><span>{hasLink ? 'Contains a link — X bills link posts at a higher rate.' : 'No link in the body.'}</span></div>
            <div className="check-row"><Icon name={item.last_error ? 'alert' : 'check'} size={14} style={{ color: item.last_error ? 'var(--bad)' : 'var(--ok)' }} /><span>{item.last_error ? `Last error: ${item.last_error}` : 'No delivery errors recorded.'}</span></div>
            <div className="check-row"><Icon name="lock" size={14} style={{ color: 'var(--muted)' }} /><span>Brand policy: {titleCase(brand.approval_policy)}</span></div>
          </div>
        </Card>
        {scope && (
          <Card>
            <CardHeader title="Exact approval scope" sub="what you are approving" />
            <div className="card-b"><KV rows={scopeSummary(scope).map((r) => ({ k: r.k, v: <span className={r.k === 'Fingerprint' ? 'mono' : ''}>{r.v}</span> }))} /></div>
          </Card>
        )}
      </div>
      {modal === 'approve' && scope && (
        <ConfirmApprove title={`Approve exact revision ${item.revision}?`} busy={busy} onClose={() => setModal(null)} onConfirm={approve}
          ack="I reviewed this exact post text and the approval scope below."
          intent="Approval is recorded against this revision only. Any edit creates a new revision and invalidates it. Nothing is posted by this action.">
          <div className="xpost" style={{ fontSize: 13 }}>{body}</div>
          <KV rows={scopeSummary(scope).map((r) => ({ k: r.k, v: <span className={r.k === 'Fingerprint' ? 'mono' : ''}>{r.v}</span> }))} />
        </ConfirmApprove>
      )}
      {modal === 'reject' && (
        <Modal title={`Reject revision ${item.revision}?`} onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button><button className="btn danger" disabled={busy} onClick={reject}>Reject revision</button></>}>
          <div className="muted">The item leaves the queue. A new revision has to be drafted and submitted to return.</div>
        </Modal>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* Newsletter issue (Beehiiv draft)                                    */
/* ------------------------------------------------------------------ */

function NewsletterReview({ issue, brand, onDone }: { issue: NewsletterIssue; brand: Brand; onDone: () => void }) {
  const { notify } = useToast()
  const evidence = useLoad(() => newsletters.factCheck(issue.id, issue.current_revision), [issue.id, issue.current_revision])
  const [modal, setModal] = useState<'approve' | 'reject' | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const readiness = useMemo(() => newsletterReviewReady(issue, evidence.data ?? null), [issue, evidence.data])
  const scope = issue.approval_scope
  const content = issue.content

  const approve = async () => {
    if (!scope) return
    setBusy(true)
    try {
      await newsletters.approve(issue.id, issue.current_revision, scope.review_token)
      notify(`Approved newsletter revision ${issue.current_revision}. Export creates an unpublished Beehiiv draft only.`)
      onDone()
    } catch (err) { notify(describe(err), 'bad') } finally { setBusy(false); setModal(null) }
  }
  const reject = async () => {
    if (!reason.trim()) { notify('A rejection reason is required; nothing changed.', 'bad'); return }
    setBusy(true)
    try {
      await newsletters.reject(issue.id, issue.current_revision, reason.trim())
      notify('Returned to draft as a new revision. Any distribution package for the old revision is invalidated.')
      onDone()
    } catch (err) { notify(describe(err), 'bad') } finally { setBusy(false); setModal(null) }
  }

  return (
    <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 1fr) 360px', gap: 16, alignItems: 'start' }}>
      <Card>
        <CardHeader title={<BrandTag brand={brand} />} sub={<span className="nowrap">Newsletter · revision {issue.current_revision} · by {content?.created_by}</span>}
          right={<><StatusChip status={issue.lifecycle} /><Link className="btn sm" to={`/content/newsletter/${encodeURIComponent(issue.id)}`}><Icon name="file" size={14} />Open</Link></>} />
        <div style={{ padding: '18px 22px' }}><NewsletterBody revision={content} /></div>
        <div style={{ padding: '10px 16px', borderTop: '1px solid var(--line-2)' }} className="stack">
          <div className="meta">Approving creates an unpublished Beehiiv draft on export. No scheduling, publishing, or sending happens from BrandMan.</div>
          {!readiness.ready && <div className="meta" style={{ color: 'var(--human)' }}>Not ready: {readiness.reasons.join('; ')}.</div>}
          <div className="row">
            <button className="btn ok" disabled={!readiness.ready || busy} onClick={() => setModal('approve')}><Icon name="check" size={15} />Approve revision {issue.current_revision}</button>
            <button className="btn" disabled={busy} onClick={() => setModal('reject')}>Reject and return for changes</button>
          </div>
        </div>
      </Card>
      <div className="stack" style={{ gap: 12 }}>
        <Card>
          <CardHeader icon="checkcircle" iconColor={evidence.data?.passed ? 'var(--ok)' : 'var(--human)'} title="Fact-check evidence" right={evidence.data ? <Chip kind={evidence.data.passed ? 'ok' : 'bad'}>{evidence.data.passed ? 'passed' : 'failed'}</Chip> : undefined} />
          <div className="card-b stack" style={{ gap: 8 }}>
            {evidence.loading && <Loading />}
            {evidence.error && <span className="meta">{evidence.error}</span>}
            {evidence.data === null && !evidence.loading && <span className="meta">No fact-check recorded for revision {issue.current_revision}.</span>}
            {evidence.data && (
              <>
                <div className="row" style={{ fontSize: 12.5 }}><span className="meta">Reviewer</span><b>{evidence.data.reviewer}</b><span className="meta">{relTime(evidence.data.created_at)}</span></div>
                {evidence.data.notes && <div className="muted" style={{ fontSize: 12.5 }}>Reviewer notes: {evidence.data.notes}</div>}
                {evidence.data.verdicts.length === 0 && <div className="meta">No claims to verify in this revision.</div>}
                {evidence.data.verdicts.map((v) => (
                  <div key={v.claim_id} className="check-row" style={{ fontSize: 12.5 }}><Icon name={v.verified ? 'check' : 'alert'} size={14} style={{ color: v.verified ? 'var(--ok)' : 'var(--bad)' }} /><span><span className="mono">{v.claim_id}</span>{v.notes ? ` — ${v.notes}` : ''}</span></div>
                ))}
                <div className="check-row" style={{ fontSize: 12.5 }}><Icon name={scope && evidence.data.content_fingerprint === scope.material_fingerprint ? 'check' : 'alert'} size={14} style={{ color: scope && evidence.data.content_fingerprint === scope.material_fingerprint ? 'var(--ok)' : 'var(--bad)' }} /><span>Evidence fingerprint {scope && evidence.data.content_fingerprint === scope.material_fingerprint ? 'matches' : 'does not match'} the approval scope</span></div>
              </>
            )}
          </div>
        </Card>
        <Card>
          <CardHeader title="Governance" right={<Chip kind={issue.governance?.reviewable ? 'ok' : 'human'}>{issue.governance?.reviewable ? 'reviewable' : 'blocked'}</Chip>} />
          <div className="card-b stack" style={{ gap: 6, fontSize: 12.5 }}>
            <div className="muted">{issue.governance?.next_safe_action}</div>
            {issue.governance?.blockers.map((b) => <div key={b.code} className="check-row"><Icon name="alert" size={14} style={{ color: 'var(--human)' }} /><span>{b.message}</span></div>)}
          </div>
        </Card>
        {scope && (
          <Card>
            <CardHeader title="Exact approval scope" sub="what you are approving" />
            <div className="card-b"><KV rows={scopeSummary(scope).map((r) => ({ k: r.k, v: <span className={r.k === 'Fingerprint' ? 'mono' : ''}>{r.v}</span> }))} /></div>
          </Card>
        )}
      </div>
      {modal === 'approve' && scope && (
        <ConfirmApprove title={`Confirm newsletter approval for exact revision ${issue.current_revision}?`} busy={busy} onClose={() => setModal(null)} onConfirm={approve}
          ack="I reviewed the complete newsletter, every claim verdict, every citation, and the approval scope."
          intent="Create an unpublished Beehiiv draft from this exact revision. No scheduling, publishing, or sending. Any edit creates a new revision and invalidates this approval.">
          <KV rows={scopeSummary(scope).map((r) => ({ k: r.k, v: <span className={r.k === 'Fingerprint' ? 'mono' : ''}>{r.v}</span> }))} />
        </ConfirmApprove>
      )}
      {modal === 'reject' && (
        <Modal title={`Reject revision ${issue.current_revision} and return for changes`} onClose={() => setModal(null)}
          footer={<><button className="btn" onClick={() => setModal(null)}>Cancel</button><button className="btn danger" disabled={busy || !reason.trim()} onClick={reject}>Reject revision</button></>}>
          <div className="field"><label>Why is it being returned? (required, shown to the drafter)</label><textarea className="input" rows={4} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} /></div>
          <div className="meta">Creates a new draft revision with your reason attached and invalidates its distribution package.</div>
        </Modal>
      )}
    </div>
  )
}

function ConfirmApprove({ title, ack, intent, busy, children, onClose, onConfirm }: { title: string; ack: string; intent: string; busy: boolean; children: React.ReactNode; onClose: () => void; onConfirm: () => void }) {
  const [checked, setChecked] = useState(false)
  return (
    <Modal title={title} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn ok" disabled={!checked || busy} onClick={onConfirm}><Icon name="check" size={14} />Approve</button></>}>
      <div className="ai-note" style={{ background: 'var(--human-soft)', color: 'var(--human)' }}><Icon name="lock" size={15} /><span>{intent}</span></div>
      {children}
      <label className="check-row" style={{ cursor: 'pointer' }}><input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} /><span>{ack}</span></label>
    </Modal>
  )
}
