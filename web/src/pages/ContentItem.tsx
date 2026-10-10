import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { brands, execution, newsletters } from '../api/endpoints'
import { NEWSLETTER_LADDER, type BrandContext, type NewsletterIssue, type Source } from '../api/types'
import { describe, useLoad } from '../api/useLoad'
import { Icon } from '../components/icons'
import { NewsletterBody } from '../components/NewsletterBody'
import { Shell } from '../components/Shell'
import { BrandTag, Card, CardHeader, Chip, Empty, ErrorState, KV, Loading, Modal, StatusChip } from '../components/ui'
import { relTime, shortDateTime, titleCase } from '../lib/format'
import { safeHttpUrl } from '../lib/review'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'

interface CompletionBundle {
  jobs: Record<string, unknown>[]
  reconciliations: Record<string, unknown>[]
  handoffs: object[]
  context: BrandContext
  guideline: Record<string, unknown> | null
}

export default function ContentItem() {
  const { id = '' } = useParams()
  const { byId, selected } = useBrands()
  const { notify } = useToast()
  const q = selected ? `?brand=${encodeURIComponent(selected)}` : ''
  const load = useLoad(async () => {
    const [issue, revisions, history] = await Promise.all([newsletters.get(id), newsletters.revisions(id), newsletters.history(id)])
    const factCheck = await newsletters.factCheck(id, issue.current_revision).catch(() => null)
    return { issue, revisions, history, factCheck }
  }, [id])
  const [tab, setTab] = useState<'material' | 'versions' | 'history' | 'delivery'>('material')
  const [revisionView, setRevisionView] = useState<number | null>(null)
  const [modal, setModal] = useState<'factcheck' | 'edit' | 'policy' | 'quickhit' | 'package' | 'abandon' | 'archive' | null>(null)
  const [busy, setBusy] = useState(false)
  const [exportPreview, setExportPreview] = useState<Record<string, unknown> | null>(null)

  const issue = load.data?.issue
  const brand = issue ? byId(issue.brand_id) : undefined
  const completion = useLoad(async () => {
    if (!issue || !brand) return null
    const [jobs, reconciliations, context, guideline, consoleData] = await Promise.all([
      newsletters.exportJobs(issue.id), newsletters.providerReconciliations(issue.id),
      brands.context(brand.slug), brands.activeGuideline(brand.slug, 'newsletter', 'beehiiv').catch(() => null),
      execution.console(brand.slug),
    ])
    const handoffs = consoleData.tasks.filter((task) => task.resource_type === 'newsletter_issue' && task.resource_id === issue.id)
    return { jobs, reconciliations, context, guideline, handoffs }
  }, [issue?.id, issue?.current_revision, brand?.slug])
  const nextTarget = useMemo<'outline' | 'draft' | null>(() => (issue?.lifecycle === 'idea' ? 'outline' : issue?.lifecycle === 'outline' ? 'draft' : null), [issue])

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true)
    try { await fn(); notify(label); load.reload(); completion.reload() } catch (err) { notify(describe(err), 'bad') } finally { setBusy(false); setModal(null) }
  }

  const title = issue?.content?.final_title || issue?.content?.working_title || 'Newsletter'
  return (
    <Shell title={title} crumb={<Link to={`/content${q}`}>Content /</Link>}>
      {load.error && <ErrorState message={load.error} retry={load.reload} />}
      {load.loading && !load.data && <Loading />}
      {issue && load.data && (
        <>
          <div className="row" style={{ gap: 14 }}>
            <BrandTag brand={brand} />
            <span className="meta">Newsletter · Beehiiv · created by {issue.content?.created_by}</span>
            <div className="stepper" style={{ marginLeft: 'auto' }}>
              {NEWSLETTER_LADDER.map((step, i) => {
                const idx = NEWSLETTER_LADDER.indexOf(issue.lifecycle)
                const cls = i < idx ? 'done' : i === idx ? 'now' : ''
                return <span key={step} className="row" style={{ gap: 0 }}><span className={`step ${cls}`}>{cls === 'done' && <Icon name="check" size={13} />}{titleCase(step)}</span>{i < NEWSLETTER_LADDER.length - 1 && <span className="step-line" />}</span>
              })}
              {(issue.lifecycle === 'abandoned' || issue.lifecycle === 'archived') && <StatusChip status={issue.lifecycle} />}
            </div>
          </div>
          <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 1fr) 340px', gap: 16, alignItems: 'start' }}>
            <Card>
              <div className="card-h" style={{ gap: 0 }}>
                <span className="tabs">
                  <button className={`tab ${tab === 'material' ? 'on' : ''}`} onClick={() => { setTab('material'); setRevisionView(null) }}>Material</button>
                  <button className={`tab ${tab === 'versions' ? 'on' : ''}`} onClick={() => setTab('versions')}>Versions ({load.data.revisions.length})</button>
                  <button className={`tab ${tab === 'history' ? 'on' : ''}`} onClick={() => setTab('history')}>History</button>
                  <button className={`tab ${tab === 'delivery' ? 'on' : ''}`} onClick={() => setTab('delivery')}>Delivery evidence</button>
                </span>
                <span className="meta" style={{ marginLeft: 'auto' }}>Revision {revisionView ?? issue.current_revision}{revisionView && revisionView !== issue.current_revision ? ' (older)' : ''}</span>
              </div>
              {tab === 'material' && <div style={{ padding: '22px 28px' }}><NewsletterBody revision={revisionView ? load.data.revisions.find((r) => r.revision === revisionView) : issue.content} /></div>}
              {tab === 'versions' && (
                <table><tbody>
                  {[...load.data.revisions].reverse().map((r) => (
                    <tr key={r.id} className="row-link" onClick={() => { setRevisionView(r.revision); setTab('material') }}>
                      <td className="mono" style={{ width: 60, color: 'var(--faint)' }}>v{r.revision}</td>
                      <td><b>{r.created_by}</b> <span className="muted">{r.change_note || 'no change note'}</span></td>
                      <td className="meta nowrap">{relTime(r.created_at)}</td>
                      <td style={{ width: 120 }}>{r.revision === issue.approved_revision && <Chip kind="ok">approved</Chip>}{r.revision === issue.current_revision && <Chip kind="neutral">current</Chip>}</td>
                    </tr>
                  ))}
                </tbody></table>
              )}
              {tab === 'history' && (
                <div>
                  {load.data.history.length === 0 && <Empty>No lifecycle events recorded.</Empty>}
                  {[...load.data.history].reverse().map((e) => (
                    <div key={e.id} className="feed-item">
                      <div className="feed-ico human"><Icon name="history" size={14} /></div>
                      <div style={{ flex: 1 }}><b>{e.actor}</b> <span className="muted">{titleCase(e.action)}{e.from_state || e.to_state ? ` · ${e.from_state ?? '∅'} → ${e.to_state ?? '∅'}` : ''}{e.revision ? ` · r${e.revision}` : ''}{e.reason ? ` — ${e.reason}` : ''}</span></div>
                      <div className="meta nowrap">{relTime(e.created_at)}</div>
                    </div>
                  ))}
                </div>
              )}
              {tab === 'delivery' && <DeliveryEvidence completion={completion.data} loading={completion.loading} preview={exportPreview} />}
            </Card>
            <div className="stack" style={{ gap: 12 }}>
              <Card>
                <CardHeader title="Details" />
                <div className="card-b"><KV rows={[
                  { k: 'Lifecycle', v: <StatusChip status={issue.lifecycle} /> },
                  { k: 'Revision', v: <span className="mono">r{issue.current_revision}</span> },
                  { k: 'Approved', v: issue.approved_revision ? `r${issue.approved_revision} by ${issue.approved_by} · ${shortDateTime(issue.approved_at)}` : 'not yet' },
                  { k: 'Beehiiv', v: issue.beehiiv_external_id ? <span className="mono">{issue.beehiiv_external_id}</span> : 'no draft exported' },
                  ...(issue.beehiiv_preview_url && safeHttpUrl(issue.beehiiv_preview_url) ? [{ k: 'Preview', v: <a href={safeHttpUrl(issue.beehiiv_preview_url)!} target="_blank" rel="noopener noreferrer">open</a> }] : []),
                  { k: 'Reader', v: issue.content?.target_reader || '—' },
                  { k: 'Outcome', v: issue.content?.intended_outcome || '—' },
                ]} /></div>
              </Card>
              <Card>
                <CardHeader title="Governance" right={<Chip kind={issue.governance?.reviewable ? 'ok' : 'human'}>{issue.governance?.reviewable ? 'reviewable' : 'blocked'}</Chip>} />
                <div className="card-b stack" style={{ gap: 6, fontSize: 12.5 }}>
                  <div className="muted">{issue.governance?.next_safe_action}</div>
                  {issue.governance?.blockers.map((b) => <div key={b.code} className="check-row"><Icon name="alert" size={14} style={{ color: 'var(--human)' }} /><span>{b.message}</span></div>)}
                  {load.data.factCheck && <div className="check-row"><Icon name={load.data.factCheck.passed ? 'check' : 'alert'} size={14} style={{ color: load.data.factCheck.passed ? 'var(--ok)' : 'var(--bad)' }} /><span>Fact-check r{load.data.factCheck.revision} by {load.data.factCheck.reviewer} · {load.data.factCheck.passed ? 'passed' : 'failed'}</span></div>}
                </div>
              </Card>
              <Card>
                <CardHeader title="Actions" sub="each one is a governed step" />
                <div className="card-b stack">
                  {!['abandoned', 'archived', 'published'].includes(issue.lifecycle) && <button className="btn" disabled={busy} onClick={() => setModal('edit')}><Icon name="edit" size={14} />Create revised material</button>}
                  {nextTarget && <button className="btn" disabled={busy} onClick={() => run(`Moved to ${nextTarget}.`, () => newsletters.transition(issue.id, nextTarget))}><Icon name="arrow" size={14} />Advance to {nextTarget}</button>}
                  {completion.data?.guideline && ['draft', 'fact_checked'].includes(issue.lifecycle) && <button className="btn" disabled={busy} onClick={() => setModal('policy')}><Icon name="checkcircle" size={14} />Review active policy for r{issue.current_revision}</button>}
                  {issue.governance?.blockers.some((blocker) => blocker.code === 'policy_minimum_word_count') && <button className="btn" disabled={busy} onClick={() => setModal('quickhit')}><Icon name="alert" size={14} />Authorize this revision as a quick hit</button>}
                  {issue.lifecycle === 'draft' && <button className="btn" disabled={busy || (issue.governance?.blockers.some((b) => b.code !== 'current_revision_fact_check_required') ?? false)} onClick={() => setModal('factcheck')}><Icon name="checkcircle" size={14} />Record fact-check for r{issue.current_revision}</button>}
                  {issue.lifecycle === 'fact_checked' && <Link className="btn ok" to={`/approvals/newsletter/${encodeURIComponent(issue.id)}${q}`}><Icon name="check" size={14} />Review for exact approval</Link>}
                  {issue.lifecycle === 'approved' && <button className="btn" disabled={busy} onClick={() => setModal('package')}><Icon name="layers" size={14} />Build distribution package</button>}
                  {issue.lifecycle === 'approved' && <button className="btn" disabled={busy} onClick={async () => { setBusy(true); try { setExportPreview(await newsletters.exportPreview(issue.id)); setTab('delivery'); notify('Exact export preview prepared; no provider action occurred.') } catch (error) { notify(describe(error), 'bad') } finally { setBusy(false) } }}><Icon name="search" size={14} />Preview exact Beehiiv export</button>}
                  {issue.lifecycle === 'approved' && <button className="btn accent" disabled={busy} onClick={() => run('Export queued. It creates an unpublished Beehiiv draft only.', () => newsletters.exportDraft(issue.id))}><Icon name="send" size={14} />Queue private Beehiiv draft</button>}
                  {['exported', 'scheduled', 'published'].includes(issue.lifecycle) && <div className="meta">Provider steps (scheduling, sending) happen in Beehiiv with a human present; BrandMan only reconciles receipts.</div>}
                  {!['abandoned', 'archived'].includes(issue.lifecycle) && <button className="btn danger" disabled={busy} onClick={() => setModal('abandon')}>Abandon issue</button>}
                  {['abandoned', 'published'].includes(issue.lifecycle) && <button className="btn" disabled={busy} onClick={() => setModal('archive')}>Archive terminal issue</button>}
                  <div className="meta">Any edit creates a new revision and invalidates prior approval, package, and unbegun handoff evidence.</div>
                </div>
              </Card>
            </div>
          </div>
          {modal === 'factcheck' && <FactCheckModal issue={issue} busy={busy} onClose={() => setModal(null)} onSubmit={(verdicts, notes) => run('Fact-check recorded.', () => newsletters.recordFactCheck(issue.id, issue.current_revision, verdicts, notes))} />}
          {modal === 'edit' && <EditModal issue={issue} busy={busy} onClose={() => setModal(null)} onSubmit={(changes, note) => run('A new revision was created; prior approval evidence is invalid.', () => newsletters.revise(issue.id, changes, note))} />}
          {modal === 'policy' && completion.data?.guideline && <PolicyModal issue={issue} guideline={completion.data.guideline} busy={busy} onClose={() => setModal(null)} onSubmit={(checklist) => run('Policy review recorded for this exact revision.', () => newsletters.policyReview(issue.id, issue.current_revision, checklist))} />}
          {modal === 'quickhit' && <ReasonModal title={`Authorize revision ${issue.current_revision} as a quick hit`} action="Authorize exact revision" minimum={12} busy={busy} onClose={() => setModal(null)} onSubmit={(reason) => run('Quick-hit exception recorded for this exact revision.', () => newsletters.authorizeQuickHit(issue.id, issue.current_revision, reason))} />}
          {modal === 'package' && completion.data && <PackageModal issue={issue} sources={completion.data.context.sources} busy={busy} onClose={() => setModal(null)} onSubmit={(payload) => run('Draft-only distribution package created.', () => newsletters.createDistributionPackage(issue.id, payload))} />}
          {modal === 'abandon' && <ReasonModal title="Abandon newsletter issue" action="Abandon issue" busy={busy} onClose={() => setModal(null)} onSubmit={(reason) => run('Newsletter issue abandoned.', () => newsletters.abandon(issue.id, reason))} />}
          {modal === 'archive' && <ReasonModal title="Archive newsletter issue" action="Archive issue" busy={busy} onClose={() => setModal(null)} onSubmit={(reason) => run('Newsletter issue archived.', () => newsletters.archive(issue.id, reason))} />}
        </>
      )}
    </Shell>
  )
}

function FactCheckModal({ issue, busy, onClose, onSubmit }: { issue: NewsletterIssue; busy: boolean; onClose: () => void; onSubmit: (verdicts: { claim_id: string; verified: boolean; notes?: string }[], notes: string) => void }) {
  const claims = issue.content?.claims ?? []
  const [verified, setVerified] = useState<Record<string, boolean>>({})
  const [notes, setNotes] = useState('')
  const ids = claims.map((c, i) => String(c.id ?? c.claim_id ?? i))
  const allVerified = ids.every((cid) => verified[cid])
  return (
    <Modal title={`Record fact-check for revision ${issue.current_revision}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !allVerified || !notes.trim()} onClick={() => onSubmit(ids.map((cid) => ({ claim_id: cid, verified: true })), notes.trim())}>Record fact-check</button></>}>
      <div className="muted">Every claim needs a positive verdict against its cited source. The reviewer is recorded from your session, never typed in. If a claim does not hold, reject the revision instead.</div>
      {claims.length === 0 && <div className="meta">This revision makes no verifiable claims; the check records that nothing needed verification.</div>}
      {claims.map((c, i) => {
        const cid = String(c.id ?? c.claim_id ?? i)
        return (
          <label key={cid} className="check-row" style={{ cursor: 'pointer' }}>
            <input type="checkbox" checked={!!verified[cid]} onChange={(e) => setVerified({ ...verified, [cid]: e.target.checked })} />
            <span><span className="mono" style={{ color: 'var(--faint)' }}>{cid}</span> {String(c.text ?? c.statement ?? '')}<div className="meta">{(c.citations ?? []).map((cit) => `source ${cit.source_id ?? '?'}`).join(', ') || 'no citations'}</div></span>
          </label>
        )
      })}
      <div className="field"><label>Reviewer notes (required)</label><textarea className="input" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="What you checked and against what" /></div>
    </Modal>
  )
}

function EditModal({ issue, busy, onClose, onSubmit }: { issue: NewsletterIssue; busy: boolean; onClose: () => void; onSubmit: (changes: Record<string, unknown>, note: string) => void }) {
  const [title, setTitle] = useState(issue.content.final_title || issue.content.working_title || '')
  const [subject, setSubject] = useState(issue.content.subject || '')
  const [preview, setPreview] = useState(issue.content.preview_text || '')
  const [note, setNote] = useState('')
  const changed = title !== (issue.content.final_title || issue.content.working_title || '') || subject !== (issue.content.subject || '') || preview !== (issue.content.preview_text || '')
  return <Modal title={`Create revision ${issue.current_revision + 1}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !changed || !note.trim()} onClick={() => onSubmit({ final_title: title, subject, preview_text: preview }, note.trim())}>Create revision</button></>}>
    <div className="ai-note"><Icon name="lock" size={16} /><div>This creates a new governed revision. Any exact approval on the current revision becomes invalid immediately.</div></div>
    <div className="field"><label htmlFor="revision-title">Title</label><input id="revision-title" className="input" value={title} onChange={(event) => setTitle(event.target.value)} /></div>
    <div className="field"><label htmlFor="revision-subject">Subject</label><input id="revision-subject" className="input" value={subject} onChange={(event) => setSubject(event.target.value)} /></div>
    <div className="field"><label htmlFor="revision-preview">Preview text</label><textarea id="revision-preview" className="input" rows={3} value={preview} onChange={(event) => setPreview(event.target.value)} /></div>
    <div className="field"><label htmlFor="revision-note">Why this changed</label><textarea id="revision-note" className="input" rows={2} value={note} onChange={(event) => setNote(event.target.value)} /></div>
  </Modal>
}

function PolicyModal({ issue, guideline, busy, onClose, onSubmit }: { issue: NewsletterIssue; guideline: Record<string, unknown>; busy: boolean; onClose: () => void; onSubmit: (checklist: Record<string, boolean>) => void }) {
  const rules = (guideline.rules ?? {}) as { operator_checklist?: string[] }
  const items = rules.operator_checklist ?? []
  const [checked, setChecked] = useState<Record<string, boolean>>({})
  const complete = items.length > 0 && items.every((item) => checked[item])
  return <Modal title={`Policy review · revision ${issue.current_revision}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !complete} onClick={() => onSubmit(checked)}>Record exact policy review</button></>}>
    <div className="muted">Confirm the active guideline checklist against this exact displayed revision. A later edit invalidates this review.</div>
    {items.length === 0 ? <Empty>The active guideline does not define an operator checklist.</Empty> : items.map((item) => <label className="check-row" key={item}><input type="checkbox" checked={!!checked[item]} onChange={(event) => setChecked((current) => ({ ...current, [item]: event.target.checked }))} /><span>{titleCase(item)}</span></label>)}
  </Modal>
}

function ReasonModal({ title, action, minimum = 1, busy, onClose, onSubmit }: { title: string; action: string; minimum?: number; busy: boolean; onClose: () => void; onSubmit: (reason: string) => void }) {
  const [reason, setReason] = useState('')
  return <Modal title={title} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn danger" disabled={busy || reason.trim().length < minimum} onClick={() => onSubmit(reason.trim())}>{action}</button></>}>
    <div className="field"><label htmlFor="governed-reason">Audited reason</label><textarea id="governed-reason" className="input" rows={4} value={reason} onChange={(event) => setReason(event.target.value)} /></div>
    <div className="meta">This decision is attributed to your authenticated session.</div>
  </Modal>
}

function PackageModal({ issue, sources, busy, onClose, onSubmit }: { issue: NewsletterIssue; sources: Source[]; busy: boolean; onClose: () => void; onSubmit: (payload: Record<string, unknown>) => void }) {
  const title = issue.content.final_title || issue.content.working_title || 'Newsletter'
  const [sourceId, setSourceId] = useState(issue.content.source_provenance?.[0]?.source_id ?? '')
  const [campaign, setCampaign] = useState(title)
  const [objective, setObjective] = useState(issue.content.editorial_thesis || issue.content.intended_outcome || '')
  const [webUrl, setWebUrl] = useState('')
  const [xBody, setXBody] = useState(`${title}\n\n${issue.content.cta?.label ?? 'Read the full issue'}`)
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 100) || `newsletter-r${issue.current_revision}`
  const submit = () => onSubmit({
    expected_revision: issue.current_revision,
    primary_source_id: sourceId,
    campaign_name: campaign,
    objective,
    email: { subject: issue.content.subject || title, preview_text: issue.content.preview_text || title },
    web: { title, slug, seo_description: issue.content.seo?.description || issue.content.preview_text || title, ...(webUrl ? { url: webUrl } : {}) },
    distribution: { audience: issue.content.target_reader || 'Newsletter readers', primary_cta: issue.content.cta?.label || 'Read the issue', measurement_plan: 'Measure aggregate delivery, opens, and clicks after provider reconciliation.' },
    x_drafts: [{ body: xBody, role: 'launch', hook: title, cta: issue.content.cta?.label || 'Read the issue', ...(webUrl ? { destination_url: webUrl } : {}) }],
    idempotency_key: `dashboard:${issue.id}:r${issue.current_revision}`,
  })
  return <Modal title={`Build draft-only distribution package · r${issue.current_revision}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy || !sourceId || !campaign.trim() || !objective.trim() || !xBody.trim()} onClick={submit}>Create draft package</button></>}>
    <div className="ai-note"><Icon name="lock" size={16} /><div>This creates draft email, web, and X assets. It does not approve, schedule, send, or publish anything.</div></div>
    <div className="field"><label htmlFor="package-source">Primary source</label><select id="package-source" className="input" value={sourceId} onChange={(event) => setSourceId(event.target.value)}><option value="">Select source evidence</option>{sources.map((source) => <option value={source.id} key={source.id}>{source.title}</option>)}</select></div>
    <div className="field"><label htmlFor="package-campaign">Campaign name</label><input id="package-campaign" className="input" value={campaign} onChange={(event) => setCampaign(event.target.value)} /></div>
    <div className="field"><label htmlFor="package-objective">Objective</label><textarea id="package-objective" className="input" rows={2} value={objective} onChange={(event) => setObjective(event.target.value)} /></div>
    <div className="field"><label htmlFor="package-url">Canonical web URL (optional)</label><input id="package-url" className="input" value={webUrl} onChange={(event) => setWebUrl(event.target.value)} placeholder="https://demo.example/…" /></div>
    <div className="field"><label htmlFor="package-x">Launch X draft</label><textarea id="package-x" className="input" rows={4} value={xBody} onChange={(event) => setXBody(event.target.value)} /></div>
  </Modal>
}

function DeliveryEvidence({ completion, loading, preview }: { completion: CompletionBundle | null; loading: boolean; preview: Record<string, unknown> | null }) {
  if (loading && !completion) return <Loading label="Loading delivery evidence…" />
  if (!completion && !preview) return <Empty>No delivery evidence is available.</Empty>
  return <div className="stack" style={{ padding: 16 }}>
    <EvidenceCard title="Exact export preview" values={preview ? [preview] : []} empty="Prepare an exact preview from the Actions panel." />
    <EvidenceCard title="Export jobs" values={completion?.jobs ?? []} empty="No API export jobs have been queued." />
    <EvidenceCard title="Assisted execution receipts" values={completion?.handoffs ?? []} empty="No exact-approved assisted handoff exists for this issue." />
    <EvidenceCard title="Provider reconciliation" values={completion?.reconciliations ?? []} empty="No Beehiiv lifecycle evidence has been reconciled." />
  </div>
}

function EvidenceCard({ title, values, empty }: { title: string; values: object[]; empty: string }) {
  return <Card><CardHeader title={title} right={<Chip kind={values.length ? 'ok' : 'neutral'}>{values.length}</Chip>} />{values.length === 0 ? <Empty>{empty}</Empty> : values.map((value, index) => <pre className="payload-preview" key={index}>{JSON.stringify(value, null, 2)}</pre>)}</Card>
}
