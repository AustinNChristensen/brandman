import { useMemo, type CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import { dispatch, newsletters } from '../api/endpoints'
import type { DispatchAudit, LifecycleEvent } from '../api/types'
import { useLoad } from '../api/useLoad'
import { Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { Bar, BrandTag, Card, CardHeader, Chip, Empty, ErrorState, Kpi, Loading } from '../components/ui'
import { brandColor } from '../lib/brands'
import { addDays, money, parseDate, relTime, titleCase } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { metersFrom, useWorkspace, type BrandBundle } from '../state/useWorkspace'

interface NeedsYou { key: string; brand: BrandBundle['brand']; title: string; kind: string; note: { text: string; kind: 'ok' | 'human' }; updated: string; to: string }
interface Activity { key: string; who: string; human: boolean; what: string; at: string }

const AGENT_HINT = /agent|system|bot|writer|research|dispatch|reconciler|scheduler|api/i

export default function Overview() {
  const ws = useWorkspace(['awaiting', 'newsletters', 'calendar', 'usage', 'workflow', 'scorecard'])
  const bundles = ws.data
  const meters = metersFrom(bundles)
  const { selected } = useBrands()
  const q = selected ? `?brand=${encodeURIComponent(selected)}` : ''

  const needs = useMemo<NeedsYou[]>(() => {
    if (!bundles) return []
    const out: NeedsYou[] = []
    for (const b of bundles) {
      for (const item of b.awaiting) {
        out.push({
          key: item.id, brand: b.brand, title: item.payload.body?.slice(0, 120) || item.id, kind: `${item.connector.toUpperCase()} ${item.approval_scope?.action_type ?? 'post'} · revision ${item.revision}`,
          note: item.last_error ? { text: 'last attempt failed', kind: 'human' } : { text: 'ready to review', kind: 'ok' },
          updated: item.updated_at, to: `/approvals/dispatch/${encodeURIComponent(item.id)}${q}`,
        })
      }
      for (const issue of b.newsletters) {
        if (issue.lifecycle !== 'fact_checked' && issue.lifecycle !== 'draft') continue
        const ready = issue.lifecycle === 'fact_checked'
        out.push({
          key: issue.id, brand: b.brand, title: issue.content?.final_title || issue.content?.working_title || 'Untitled newsletter', kind: `Newsletter · revision ${issue.current_revision}`,
          note: ready ? { text: 'fact-checked · ready to review', kind: 'ok' } : { text: issue.governance?.fact_check_valid ? 'draft in progress' : 'needs fact-check', kind: 'human' },
          updated: issue.updated_at, to: ready ? `/approvals/newsletter/${encodeURIComponent(issue.id)}${q}` : `/content/newsletter/${encodeURIComponent(issue.id)}${q}`,
        })
      }
    }
    return out.sort((a, b) => (parseDate(a.updated)?.getTime() ?? 0) - (parseDate(b.updated)?.getTime() ?? 0))
  }, [bundles, q])

  const stats = useMemo(() => {
    if (!bundles) return null
    const now = new Date(), weekAhead = addDays(now, 7), weekBack = addDays(now, -7)
    let scheduled = 0, published = 0, spend = 0, priced = false
    for (const b of bundles) {
      for (const c of b.calendar) {
        const d = parseDate(c.scheduled_for)
        if (!d) continue
        if (c.status === 'scheduled' && d >= now && d <= weekAhead) scheduled++
        if (c.status === 'published' && d >= weekBack && d <= now) published++
      }
      for (const t of b.usage?.totals ?? []) if (t.estimated_cost !== null && t.currency === 'USD') { spend += Number(t.estimated_cost); priced = true }
    }
    const oldest = needs[0] ? relTime(needs[0].updated) : null
    const unpriced = bundles.reduce((n, b) => n + (b.usage?.unpriced_request_count ?? 0), 0)
    return { scheduled, published, spend, priced, oldest, unpriced }
  }, [bundles, needs])

  const activity = useLoad<Activity[]>(async () => {
    if (!bundles) return []
    const feed: Activity[] = []
    await Promise.all(bundles.flatMap((b) => [
      ...b.awaiting.slice(0, 8).map(async (item) => {
        const audit = await dispatch.audit(item.id).catch(() => [] as DispatchAudit[])
        for (const e of audit) feed.push({ key: `${item.id}:${e.at}:${e.action}`, who: e.actor, human: !AGENT_HINT.test(e.actor), what: `${titleCase(e.action)} · ${b.brand.name} · “${(item.payload.body ?? item.id).slice(0, 48)}…”${e.detail ? ` — ${e.detail}` : ''}`, at: e.at })
      }),
      ...b.newsletters.slice(0, 6).map(async (issue) => {
        const history = await newsletters.history(issue.id).catch(() => [] as LifecycleEvent[])
        for (const e of history) feed.push({ key: `${issue.id}:${e.id}`, who: e.actor, human: !AGENT_HINT.test(e.actor), what: `${titleCase(e.action)}${e.to_state ? ` → ${e.to_state}` : ''} · ${issue.content?.final_title || 'newsletter'}${e.reason ? ` — ${e.reason}` : ''}`, at: e.created_at })
      }),
    ]))
    return feed.sort((a, b) => (parseDate(b.at)?.getTime() ?? 0) - (parseDate(a.at)?.getTime() ?? 0)).slice(0, 14)
  }, [bundles])

  return (
    <Shell title="Overview" crumb={new Date().toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })} meters={meters}
      right={<Chip kind="agent" icon="lock">human approval required</Chip>}>
      {ws.error && <ErrorState message={ws.error} retry={ws.reload} />}
      {ws.loading && !bundles && <Loading />}
      {bundles && stats && (
        <>
          <div className="grid overview-kpis">
            <Kpi label="Awaiting your approval" value={needs.length} sub={needs.length ? `oldest ${stats.oldest}` : 'queue is clear'} color={needs.length ? 'var(--human)' : undefined} to={`/approvals${q}`} />
            <Kpi label="Scheduled next 7 days" value={stats.scheduled} sub={`across ${bundles.length} brand${bundles.length === 1 ? '' : 's'}`} to={`/planner${q}`} />
            <Kpi label="Published last 7 days" value={stats.published} sub="from the content calendar" to={`/planner${q}`} />
            <Kpi label="Estimated spend (month)" value={stats.priced ? money(stats.spend) : '—'} sub={stats.priced ? `${stats.unpriced} unpriced requests` : 'add provider rate cards to price usage'} />
          </div>
          <div className="grid overview-main">
            <div className="stack" style={{ gap: 16 }}>
              <Card>
                <CardHeader icon="checkcircle" iconColor="var(--human)" title="Needs you" sub="— human approval gate" right={<Link className="btn sm" to={`/approvals${q}`}>Open queue <Icon name="arrow" size={14} /></Link>} />
                {needs.length === 0 ? <Empty>Nothing is waiting on you.</Empty> : (
                  <table><tbody>
                    {needs.slice(0, 8).map((n) => (
                      <tr key={n.key}>
                        <td style={{ width: 70 }}><BrandTag brand={n.brand} short /></td>
                        <td style={{ width: '100%', maxWidth: 0 }}><div style={{ fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{n.title}</div><div className="meta">{n.kind}</div></td>
                        <td className="nowrap"><Chip kind={n.note.kind}>{n.note.text}</Chip></td>
                        <td className="meta nowrap">{relTime(n.updated)}</td>
                        <td style={{ textAlign: 'right' }}><Link className="btn sm" to={n.to}>Review</Link></td>
                      </tr>
                    ))}
                  </tbody></table>
                )}
              </Card>
              <div className="grid overview-brands" style={{ '--brand-cols': Math.min(3, Math.max(1, bundles.length)) } as CSSProperties}>
                {bundles.map((b) => <BrandCard key={b.brand.id} bundle={b} q={q} />)}
              </div>
            </div>
            <Card>
              <CardHeader icon="bot" iconColor="var(--accent)" title="Activity" sub="audit trail" />
              {activity.loading && <Loading />}
              {activity.data && activity.data.length === 0 && <Empty>No recorded actions yet.</Empty>}
              {activity.data?.map((a) => (
                <div className="feed-item" key={a.key}>
                  <div className={`feed-ico ${a.human ? 'human' : ''}`}><Icon name={a.human ? 'user' : 'bot'} size={14} /></div>
                  <div style={{ flex: 1, minWidth: 0 }}><span style={{ fontWeight: 600 }}>{a.who}</span> <span className="muted">{a.what}</span></div>
                  <div className="meta nowrap">{relTime(a.at)}</div>
                </div>
              ))}
            </Card>
          </div>
        </>
      )}
    </Shell>
  )
}

function BrandCard({ bundle, q }: { bundle: BrandBundle; q: string }) {
  const { brand, workflow, scorecard } = bundle
  const color = brandColor(brand.slug)
  return (
    <Card style={{ padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div className="row" style={{ justifyContent: 'space-between' }}><BrandTag brand={brand} /><Link to={`/guidelines${q || '?'}${q ? '&' : ''}brand=${brand.slug}`} className="meta">guidelines</Link></div>
      {workflow && (
        <div className="stack" style={{ gap: 6 }}>
          <div className="row" style={{ justifyContent: 'space-between' }} >
            <span className="meta">Operator loop</span><span className="mono">{workflow.completed_steps}/{workflow.total_steps}</span>
          </div>
          <Bar pct={workflow.progress_percent} color={color} />
          <div style={{ fontSize: 12.5 }}><span className="chip agent" style={{ marginRight: 6 }}><Icon name="spark" size={12} />next</span>{workflow.next_action.text}</div>
        </div>
      )}
      {scorecard && scorecard.goals.length > 0 && (
        <div className="stack" style={{ gap: 6 }}>
          <div className="meta">{scorecard.mission_name}</div>
          {scorecard.goals.map((g) => (
            <div key={g.metric} className="row" style={{ justifyContent: 'space-between', fontSize: 12.5 }}>
              <span>{titleCase(g.metric)}</span>
              <span className="mono">{g.current} / {g.target}</span>
              <Chip kind={g.trajectory_status === 'behind' ? 'human' : 'ok'}>{g.trajectory_status}</Chip>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
