import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { publishingPlan as plannerApi } from '../api/endpoints'
import { describe, useLoad } from '../api/useLoad'
import type { Brand, CalendarItem, PublishingPlan, PublishingPlanItem, PublishingReflowCommit, PublishingReflowPreview, PublishingWindow } from '../api/types'
import { ChannelIcon, Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { BrandTag, Card, Chip, Empty, ErrorState, Loading } from '../components/ui'
import { calendarFor } from '../lib/calendar'
import { addDays, parseDate, sameDay, shortTime, startOfWeek } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'
import { metersFrom, useWorkspace } from '../state/useWorkspace'

const STATUS_STYLE: Record<string, { bg: string; fg: string }> = {
  published: { bg: 'var(--ok-soft)', fg: 'var(--ok)' },
  scheduled: { bg: 'var(--info-soft)', fg: 'var(--info)' },
  approved: { bg: 'var(--info-soft)', fg: 'var(--info)' },
  awaiting_approval: { bg: 'var(--human-soft)', fg: 'var(--human)' },
  draft: { bg: 'var(--line-2)', fg: 'var(--muted)' },
}

export default function Planner() {
  const ws = useWorkspace(['calendar', 'awaiting', 'newsletters', 'usage'])
  const [offset, setOffset] = useState(0)
  const [channel, setChannel] = useState('all')
  const { selected } = useBrands()
  const q = selected ? `?brand=${encodeURIComponent(selected)}` : ''
  const week = useMemo(() => {
    const start = addDays(startOfWeek(new Date()), offset * 7)
    return Array.from({ length: 7 }, (_, i) => addDays(start, i))
  }, [offset])
  const today = new Date()
  const channels = useMemo(() => {
    const set = new Set<string>()
    for (const b of ws.data ?? []) for (const c of calendarFor(b)) set.add(c.channel)
    return [...set].sort()
  }, [ws.data])
  const visible = (c: CalendarItem) => channel === 'all' || c.channel === channel

  return (
    <Shell title="Planner" crumb={`Week of ${week[0].toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`} meters={metersFrom(ws.data)}>
      <div className="row">
        <button className="btn ghost" onClick={() => setOffset(offset - 1)} aria-label="Previous week"><Icon name="chevl" size={16} /></button>
        <button className="btn" onClick={() => setOffset(0)}><Icon name="calendar" size={15} />This week</button>
        <button className="btn ghost" onClick={() => setOffset(offset + 1)} aria-label="Next week"><Icon name="chev" size={16} /></button>
        <label className="btn ghost" style={{ gap: 6 }}><Icon name="filter" size={14} />Channel
          <select value={channel} onChange={(e) => setChannel(e.target.value)} style={{ border: 0, background: 'transparent' }}>
            <option value="all">All</option>
            {channels.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <span className="meta" style={{ marginLeft: 'auto' }}>Nothing leaves the planner without your approval.</span>
      </div>
      <div className="meta">Coordinate initiative timing here, then use the governed approval and execution flows separately when content is ready.</div>
      <PlanOperations weekStart={week[0]} />
      {ws.error && <ErrorState message={ws.error} retry={ws.reload} />}
      {ws.loading && !ws.data && <Loading />}
      {ws.data && (
        <Card style={{ overflow: 'hidden' }}>
          <div className="plan-grid">
            <div className="plan-head">Brand</div>
            {week.map((d) => <div key={d.toISOString()} className={`plan-head ${sameDay(d, today) ? 'today' : ''}`}>{d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' })}</div>)}
          </div>
          {ws.data.length === 0 && <Empty>No brands.</Empty>}
          {ws.data.map((b) => {
            const calendar = calendarFor(b)
            const dated = calendar.filter(visible).filter((c) => parseDate(c.scheduled_for))
            const inWeek = dated.filter((c) => { const d = parseDate(c.scheduled_for)!; return d >= week[0] && d < addDays(week[6], 1) })
            const undated = calendar.filter(visible).filter((c) => !parseDate(c.scheduled_for))
            return (
              <div className="plan-lane" key={b.brand.id}>
                <div className="plan-brand">
                  <BrandTag brand={b.brand} />
                  <span className="meta">{inWeek.length} this week · {undated.length} unscheduled</span>
                  {b.awaiting.length > 0 && <Link className="meta" to={`/approvals${q}`} style={{ color: 'var(--human)' }}>{b.awaiting.length} awaiting approval</Link>}
                </div>
                {week.map((d) => (
                  <div key={d.toISOString()} className="plan-cell">
                    {inWeek.filter((c) => sameDay(parseDate(c.scheduled_for)!, d)).map((c) => <PlanCard key={`${c.item_type}:${c.id}`} item={c} q={q} />)}
                  </div>
                ))}
              </div>
            )
          })}
        </Card>
      )}
      {ws.data && ws.data.some((b) => calendarFor(b).some((c) => !parseDate(c.scheduled_for) && visible(c))) && (
        <Card>
          <div className="card-h">Unscheduled <span className="sub">— drafts and sources without a date</span></div>
          <table><tbody>
            {ws.data.flatMap((b) => calendarFor(b).filter((c) => !parseDate(c.scheduled_for) && visible(c)).slice(0, 12).map((c) => (
              <tr key={`${b.brand.id}:${c.item_type}:${c.id}`}>
                <td style={{ width: 70 }}><BrandTag brand={b.brand} short /></td>
                <td><div className="row" style={{ gap: 6 }}><ChannelIcon channel={c.channel} /><span style={{ fontWeight: 500 }}>{c.title}</span><span className="meta">{c.item_type}</span></div>{c.body_summary && <div className="meta" style={{ maxWidth: 720, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.body_summary}</div>}</td>
                <td><span className="chip neutral">{c.status.replace(/_/g, ' ')}</span></td>
              </tr>
            )))}
          </tbody></table>
        </Card>
      )}
      <div className="row meta" style={{ gap: 14 }}>
        {Object.entries(STATUS_STYLE).map(([k, v]) => <span key={k} className="row" style={{ gap: 6 }}><span className="dot" style={{ background: v.fg }} />{k.replace(/_/g, ' ')}</span>)}
      </div>
    </Shell>
  )
}

interface PlanBundle { brand: Brand; plan: PublishingPlan }

function PlanOperations({ weekStart }: { weekStart: Date }) {
  const { active } = useBrands()
  const { notify } = useToast()
  const [busy, setBusy] = useState('')
  const plans = useLoad<PlanBundle[]>(() => Promise.all(active.map(async (brand) => ({
    brand, plan: await plannerApi.get(brand.slug),
  }))), [active.map((brand) => brand.slug).join('|')])
  const mutate = async (key: string, action: () => Promise<unknown>, success: string) => {
    setBusy(key)
    try { await action(); notify(success); plans.reload() }
    catch (error) { notify(describe(error), 'bad') }
    finally { setBusy('') }
  }
  return <div className="stack" style={{ gap: 12 }}>
    <Card>
      <div className="card-b row" style={{ justifyContent: 'space-between' }}>
        <div><b>Publishing plan</b><div className="meta">Planning metadata only. These controls never approve, schedule with a provider, or publish.</div></div>
        <Chip kind="info">safe preview + exact undo</Chip>
      </div>
    </Card>
    {plans.error && <ErrorState message={plans.error} retry={plans.reload} />}
    {plans.loading && !plans.data && <Loading label="Loading publishing plans…" />}
    {plans.data?.map((bundle) => <BrandPlan key={`${bundle.brand.id}:${bundle.plan.settings.updated_at ?? 'default'}`}
      bundle={bundle} weekStart={weekStart} busy={busy} mutate={mutate} />)}
  </div>
}

function BrandPlan({ bundle, weekStart, busy, mutate }: {
  bundle: PlanBundle; weekStart: Date; busy: string
  mutate: (key: string, action: () => Promise<unknown>, success: string) => Promise<void>
}) {
  const { brand, plan } = bundle
  const [timezone, setTimezone] = useState(plan.settings.timezone)
  const [windows, setWindows] = useState<PublishingWindow[]>(plan.settings.windows)
  const [xCadence, setXCadence] = useState(plan.settings.cadence_minutes.x ?? 120)
  const [newsletterCadence, setNewsletterCadence] = useState(plan.settings.cadence_minutes.newsletter ?? 1440)
  const [startAt, setStartAt] = useState(toLocalInput(weekStart))
  const [preview, setPreview] = useState<PublishingReflowPreview | null>(null)
  const [commit, setCommit] = useState<PublishingReflowCommit | null>(null)
  const updateWindow = (index: number, change: Partial<PublishingWindow>) =>
    setWindows(windows.map((window, position) => position === index ? { ...window, ...change } : window))
  const previewPlan = async () => {
    setCommit(null)
    await mutate(`preview:${brand.id}`, async () => {
      const result = await plannerApi.preview(brand.slug, new Date(startAt).toISOString())
      setPreview(result)
    }, 'Reflow preview created. Nothing was scheduled or published.')
  }
  return <Card>
    <div className="card-h"><BrandTag brand={brand} /><span style={{ marginLeft: 'auto' }} className="meta">{plan.initiatives.length} initiatives</span></div>
    <div className="card-b stack" style={{ gap: 14 }}>
      <div className="grid" style={{ gridTemplateColumns: 'minmax(220px, .7fr) minmax(360px, 1.3fr)', alignItems: 'start' }}>
        <div className="stack" style={{ gap: 8 }}>
          <label className="meta">Timezone<input className="input" aria-label={`Timezone for ${brand.name}`} value={timezone} onChange={(event) => setTimezone(event.target.value)} /></label>
          <div className="row">
            <label className="meta">X spacing (minutes)<input className="input" type="number" min="0" max="43200" value={xCadence} onChange={(event) => setXCadence(Number(event.target.value))} /></label>
            <label className="meta">Newsletter spacing<input className="input" type="number" min="0" max="43200" value={newsletterCadence} onChange={(event) => setNewsletterCadence(Number(event.target.value))} /></label>
          </div>
        </div>
        <div className="stack" style={{ gap: 6 }}>
          <b>Publishing windows</b>
          {windows.map((window, index) => <div className="row" key={`${index}:${window.weekday}`}>
            <select className="input" aria-label={`Window ${index + 1} day`} value={window.weekday} onChange={(event) => updateWindow(index, { weekday: Number(event.target.value) })}>
              {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day, dayIndex) => <option key={day} value={dayIndex}>{day}</option>)}
            </select>
            <input className="input" aria-label={`Window ${index + 1} start`} type="time" value={window.start} onChange={(event) => updateWindow(index, { start: event.target.value })} />
            <span className="meta">to</span>
            <input className="input" aria-label={`Window ${index + 1} end`} type="time" value={window.end} onChange={(event) => updateWindow(index, { end: event.target.value })} />
            <button className="btn sm ghost" disabled={windows.length === 1} onClick={() => setWindows(windows.filter((_, position) => position !== index))}>Remove</button>
          </div>)}
          <div className="row">
            <button className="btn sm ghost" onClick={() => setWindows([...windows, { weekday: 0, start: '09:00', end: '17:00' }])}>Add window</button>
            <button className="btn sm" disabled={busy === `settings:${brand.id}`} onClick={() => void mutate(
              `settings:${brand.id}`,
              () => plannerApi.updateSettings(brand.slug, { timezone, windows, cadence_minutes: { ...plan.settings.cadence_minutes, x: xCadence, newsletter: newsletterCadence } }),
              'Publishing-plan settings saved. Provider schedules were not changed.',
            )}>Save settings</button>
          </div>
        </div>
      </div>
      <div className="stack" style={{ gap: 10 }}>
        {plan.initiatives.length === 0 && <Empty>No posts or newsletter issues are available to plan.</Empty>}
        {plan.initiatives.map((initiative) => <div key={initiative.id} style={{ border: '1px solid var(--line-2)', borderRadius: 10, overflow: 'hidden' }}>
          <div className="row" style={{ padding: '9px 12px', background: 'var(--panel-2)' }}><b>Initiative</b><span className="mono meta">{initiative.id}</span><Chip kind="neutral">{initiative.items.length} items</Chip></div>
          {initiative.items.map((item) => <PlannerItemRow key={`${item.item_type}:${item.item_id}`} brand={brand} item={item} busy={busy} mutate={mutate} />)}
        </div>)}
      </div>
      <div style={{ borderTop: '1px solid var(--line-2)', paddingTop: 12 }} className="stack">
        <div className="row"><label className="meta">Reflow begins<input className="input" type="datetime-local" value={startAt} onChange={(event) => setStartAt(event.target.value)} /></label>
          <button className="btn" disabled={!startAt || busy === `preview:${brand.id}`} onClick={() => void previewPlan()}>Preview reflow</button></div>
        {preview && <div className="stack" style={{ gap: 8 }}>
          <div className="row"><b>Proposed changes</b><Chip kind={preview.changes.length ? 'info' : 'ok'}>{preview.changes.length}</Chip><span className="meta">Snapshot-bound; later edits invalidate this preview.</span></div>
          {preview.changes.map((change) => <div className="meta" key={`${change.item_type}:${change.item_id}`}>{change.channel.toUpperCase()} · {change.item_id}: {change.before ? new Date(change.before).toLocaleString() : 'Unplanned'} → {new Date(change.after).toLocaleString()}</div>)}
          <div className="row"><button className="btn" disabled={!preview.changes.length || busy === `commit:${brand.id}`} onClick={() => {
            if (!window.confirm(`Apply ${preview.changes.length} planning changes for ${brand.name}? This does not publish.`)) return
            void mutate(`commit:${brand.id}`, async () => { const result = await plannerApi.commit(brand.slug, preview.id); setCommit(result); setPreview(null) }, 'Reflow committed to the plan. Nothing was published.')
          }}>Commit planning changes</button></div>
        </div>}
        {commit && !commit.undone_at && <div className="row"><span className="meta">Committed {commit.changes.length} changes. Exact undo is available while those slots remain unchanged.</span>
          <button className="btn sm ghost" disabled={busy === `undo:${brand.id}`} onClick={() => {
            if (!window.confirm(`Restore the exact pre-reflow plan for ${brand.name}?`)) return
            void mutate(`undo:${brand.id}`, async () => { const result = await plannerApi.undo(brand.slug, commit.id); setCommit(result) }, 'Exact reflow undo completed.')
          }}>Undo exact reflow</button></div>}
      </div>
    </div>
  </Card>
}

function PlannerItemRow({ brand, item, busy, mutate }: {
  brand: Brand; item: PublishingPlanItem; busy: string
  mutate: (key: string, action: () => Promise<unknown>, success: string) => Promise<void>
}) {
  const key = `${brand.id}:${item.item_type}:${item.item_id}`
  const update = (next: PublishingPlanItem, message: string) => void mutate(key, () => plannerApi.updateItem(brand.slug, next), message)
  return <div className="row" style={{ padding: '9px 12px', borderTop: '1px solid var(--line-2)' }}>
    <ChannelIcon channel={item.channel} /><div style={{ minWidth: 0, flex: 1 }}><b>{item.title}</b><div className="meta">{item.item_type} · {item.status} · {item.planned_for ? new Date(item.planned_for).toLocaleString() : 'unplanned'}</div></div>
    {item.pinned && <Chip kind="info">pinned</Chip>}{item.locked && <Chip kind="human">locked</Chip>}
    <button className="btn sm ghost" disabled={item.locked || busy === key} onClick={() => update({ ...item, pinned: !item.pinned }, item.pinned ? 'Item unpinned.' : 'Item pinned; reflow will leave it in place.')}>{item.pinned ? 'Unpin' : 'Pin'}</button>
    <button className="btn sm ghost" disabled={busy === key} onClick={() => update({ ...item, locked: !item.locked }, item.locked ? 'Item unlocked.' : 'Item locked against planning changes.')}>{item.locked ? 'Unlock' : 'Lock'}</button>
  </div>
}

function toLocalInput(value: Date) {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

function PlanCard({ item, q }: { item: CalendarItem; q: string }) {
  const style = STATUS_STYLE[item.status] ?? STATUS_STYLE.draft
  const body = (
    <div className="pcard" style={{ background: style.bg, color: style.fg }}>
      <div className="pc-meta"><ChannelIcon channel={item.channel} /><span>{item.status.replace(/_/g, ' ')}</span><span className="nowrap" style={{ marginLeft: 'auto', textTransform: 'none' }}>{shortTime(item.scheduled_for)}</span></div>
      <div className="pc-title">{item.item_type === 'post' ? (item.body_summary || item.title) : item.title}</div>
    </div>
  )
  if (item.item_type === 'newsletter') return <Link to={`/content/newsletter/${encodeURIComponent(item.id)}${q}`} style={{ color: 'inherit' }}>{body}</Link>
  return item.item_type === 'post' ? <Link to={`/content${q}`} style={{ color: 'inherit' }}>{body}</Link> : body
}
