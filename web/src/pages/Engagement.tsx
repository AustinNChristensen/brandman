import { useState } from 'react'
import { Link } from 'react-router-dom'
import { engagement } from '../api/endpoints'
import type { EngagementOpportunity } from '../api/types'
import { describe, useLoad } from '../api/useLoad'
import { ChannelIcon, Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { Card, CardHeader, Chip, Empty, ErrorState, Loading, Modal, StatusChip } from '../components/ui'
import { relTime, shortDateTime, titleCase } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'

type InboxFilter = 'all' | EngagementOpportunity['state']
type Dialog = 'draft' | 'dismiss' | null

export default function Engagement() {
  const { selected, active } = useBrands()
  const brand = selected ? active[0] : undefined
  const [filter, setFilter] = useState<InboxFilter>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [dialog, setDialog] = useState<Dialog>(null)
  const load = useLoad(
    () => brand ? engagement.list(brand.slug, filter === 'all' ? undefined : filter) : Promise.resolve([]),
    [brand?.slug, filter],
  )
  const effectiveId = selectedId && load.data?.some((item) => item.id === selectedId)
    ? selectedId : load.data?.[0]?.id ?? null
  const detail = useLoad(async () => {
    if (!brand || !effectiveId) return null
    const [opportunity, history] = await Promise.all([
      engagement.get(brand.slug, effectiveId), engagement.history(brand.slug, effectiveId),
    ])
    return { opportunity, history }
  }, [brand?.slug, effectiveId])
  const { notify } = useToast()
  const opportunity = detail.data?.opportunity ?? load.data?.find((item) => item.id === effectiveId)
  const refresh = () => { load.reload(); detail.reload() }
  const act = async (message: string, action: () => Promise<unknown>) => {
    try { await action(); notify(message); setDialog(null); refresh() }
    catch (error) { notify(describe(error), 'bad') }
  }

  if (!selected) return <Shell title="Engagement"><Card><Empty>Select one brand to review its engagement inbox.</Empty></Card></Shell>
  return <Shell title="Engagement" crumb={brand?.name} right={<Chip kind="human" icon="lock">draft → human approval · never direct send</Chip>}>
    {load.error && <ErrorState message={load.error} retry={load.reload} />}
    {load.loading && !load.data && <Loading label="Loading engagement opportunities…" />}
    {load.data && <>
      <div className="ai-note"><Icon name="lock" size={16} /><span>Every action becomes a governed dispatch draft. Submission only moves it to Approvals; it cannot send, publish, like, or follow from this screen.</span></div>
      <div className="tabs" aria-label="Engagement state filter">
        {(['all', 'new', 'drafted', 'awaiting_approval', 'needs_attention', 'dismissed'] as InboxFilter[]).map((state) =>
          <button key={state} className={`tab ${filter === state ? 'on' : ''}`} onClick={() => { setFilter(state); setSelectedId(null) }}>{titleCase(state)}</button>)}
      </div>
      <div className="engagement-layout">
        <Card className="engagement-list"><CardHeader icon="mail" title="Ranked inbox" sub={`${load.data.length} opportunities`} />
          {!load.data.length ? <Empty>No opportunities in this state.</Empty> : load.data.map((item) => <button key={item.id} className={`list-item ${opportunity?.id === item.id ? 'on' : ''}`} onClick={() => setSelectedId(item.id)}>
            <div className="row" style={{ justifyContent: 'space-between' }}><div className="row"><ChannelIcon channel="x" /><b>@{item.author.username ?? 'unknown'}</b></div><StatusChip status={item.state} /></div>
            <div className="engagement-snippet">{item.text}</div>
            <div className="meta">score {item.ranking_score} · {titleCase(item.opportunity_type)} · {relTime(item.last_seen_at)}</div>
          </button>)}
        </Card>
        <Card>{detail.error ? <ErrorState message={detail.error} retry={detail.reload} /> : !opportunity ? <Empty>Select an opportunity.</Empty> : <>
          <CardHeader icon="file" title={`@${opportunity.author.username ?? 'unknown'}`} sub={`${titleCase(opportunity.opportunity_type)} · score ${opportunity.ranking_score}`} right={<StatusChip status={opportunity.state} />} />
          <div className="card-b stack">
            <div className="xpost">{opportunity.text}</div>
            {safeHref(opportunity.thread_context.external_url) && <a href={safeHref(opportunity.thread_context.external_url)!} target="_blank" rel="noreferrer">Open source post ↗</a>}
            {!!opportunity.thread_context.parent_context?.length && <div><div className="meta">Thread context</div>{opportunity.thread_context.parent_context.map((item, index) => <div className="engagement-context" key={item.id ?? index}>{item.text}</div>)}</div>}
            <div className="row" style={{ flexWrap: 'wrap' }}>{opportunity.ranking_reasons.map((reason) => <Chip key={reason} kind="neutral">{reason}</Chip>)}</div>
            {opportunity.resurfaced_count > 0 && <div className="ai-note"><Icon name="alert" size={15} />Source context changed {opportunity.resurfaced_count} time(s). Any linked draft is stale and cannot be silently reused.</div>}
            <div className="row" style={{ flexWrap: 'wrap' }}>
              {['new'].includes(opportunity.state) && <button className="btn primary" onClick={() => setDialog('draft')}><Icon name="edit" size={14} />Draft governed action</button>}
              {opportunity.state === 'drafted' && <button className="btn primary" onClick={() => window.confirm('Submit this draft for human approval? This does not execute it.') && void act('Draft submitted for human approval.', () => engagement.submit(brand!.slug, opportunity.id))}>Submit for approval</button>}
              {opportunity.state === 'awaiting_approval' && <Link className="btn" to={`/approvals/dispatch/${encodeURIComponent(opportunity.dispatch_item_id ?? '')}?brand=${encodeURIComponent(brand!.slug)}`}>Review exact draft</Link>}
              {['new', 'drafted', 'needs_attention'].includes(opportunity.state) && <button className="btn danger" onClick={() => setDialog('dismiss')}>Dismiss</button>}
            </div>
            {opportunity.dispatch_item_id && <div className="meta">Governed dispatch <span className="mono">{opportunity.dispatch_item_id}</span> · approval required</div>}
          </div>
          <div className="card-b"><div className="meta" style={{ marginBottom: 8 }}>Immutable history</div>{detail.data?.history.map((event) => <div className="feed-item" key={event.id}><div><b>{titleCase(event.action)}</b><div className="meta">{event.actor} · {shortDateTime(event.created_at)}</div></div></div>)}</div>
        </>}</Card>
      </div>
    </>}
    {dialog === 'draft' && opportunity && <DraftModal opportunity={opportunity} close={() => setDialog(null)} submit={(kind, text) => act('Governed action drafted. No external action occurred.', () => engagement.draft(brand!.slug, opportunity.id, kind, text))} />}
    {dialog === 'dismiss' && opportunity && <DismissModal close={() => setDialog(null)} submit={(reason) => act('Opportunity dismissed. History was retained.', () => engagement.dismiss(brand!.slug, opportunity.id, reason))} />}
  </Shell>
}

function DraftModal({ opportunity, close, submit }: { opportunity: EngagementOpportunity; close: () => void; submit: (kind: 'reply' | 'like' | 'follow', text?: string) => Promise<void> }) {
  const [kind, setKind] = useState<'reply' | 'like' | 'follow'>('reply'), [text, setText] = useState('')
  const valid = kind !== 'reply' || Boolean(text.trim())
  return <Modal title={`Draft action for @${opportunity.author.username ?? 'unknown'}`} onClose={close} footer={<><button className="btn" onClick={close}>Cancel</button><button className="btn primary" disabled={!valid} onClick={() => void submit(kind, kind === 'reply' ? text : undefined)}>Create draft only</button></>}>
    <label className="field"><span>Action</span><select aria-label="Action" className="input" value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="reply">Reply</option><option value="like">Like</option><option value="follow" disabled={!opportunity.author.id}>Follow</option></select></label>
    {kind === 'reply' && <label className="field"><span>Reply draft</span><textarea aria-label="Reply draft" className="input" rows={5} maxLength={280} value={text} onChange={(event) => setText(event.target.value)} /></label>}
    <div className="meta">Anti-spam limits and duplicate-similarity checks run before a draft is created. Execution remains behind exact human approval.</div>
  </Modal>
}

function DismissModal({ close, submit }: { close: () => void; submit: (reason: string) => Promise<void> }) {
  const [reason, setReason] = useState('')
  return <Modal title="Dismiss opportunity" onClose={close} footer={<><button className="btn" onClick={close}>Cancel</button><button className="btn danger" disabled={!reason.trim()} onClick={() => void submit(reason)}>Dismiss with reason</button></>}><label className="field"><span>Reason</span><textarea aria-label="Reason" className="input" rows={3} value={reason} onChange={(event) => setReason(event.target.value)} /></label><div className="meta">Dismissal is reversible only if materially new provider context resurfaces the opportunity.</div></Modal>
}

function safeHref(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try { const url = new URL(value); return url.protocol === 'https:' ? url.toString() : null } catch { return null }
}
