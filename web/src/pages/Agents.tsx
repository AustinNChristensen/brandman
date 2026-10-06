import { useMemo, useState } from 'react'
import { agents as api, execution } from '../api/endpoints'
import { describe, useLoad } from '../api/useLoad'
import type { Brand, ExecutionAgent, ExecutionConsole, ExecutionControls, LiveReadiness } from '../api/types'
import { Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { Bar, BrandTag, Card, CardHeader, Chip, Empty, ErrorState, Loading } from '../components/ui'
import { relTime, titleCase } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'

interface AgentBundle { brand: Brand; readiness: LiveReadiness; console: ExecutionConsole; controls: ExecutionControls }

function recoveryItems(console: ExecutionConsole) {
  const tasks = console.tasks
    .filter((task) => task.operator_next_action.code !== 'receipt_complete')
    .map((task) => ({ key: `task:${task.id}`, provider: task.provider,
      title: titleCase(task.operator_next_action.code), text: task.operator_next_action.text,
      urgent: task.status === 'needs_attention' || task.status === 'stale' }))
  const pulls = console.beehiiv_aggregate_pulls
    .filter((pull) => pull.status === 'failed')
    .map((pull) => ({ key: `pull:${pull.id}`, provider: 'beehiiv',
      title: 'Recover aggregate measurement',
      text: `The read-only measurement pull failed${pull.last_failure_code ? ` (${pull.last_failure_code})` : ''}. Retry it from a fresh helper claim; never collect subscriber records.`, urgent: true }))
  return [...tasks, ...pulls]
}

export default function Agents() {
  const { active, selected } = useBrands()
  const { notify } = useToast()
  const [agentId, setAgentId] = useState('demobrand-browser')
  const [channel, setChannel] = useState<'browser' | 'mcp'>('browser')
  const [busy, setBusy] = useState('')
  const load = useLoad<AgentBundle[]>(() => Promise.all(active.map(async (brand) => {
    const [readiness, console, controls] = await Promise.all([
      api.readiness(brand.slug), execution.console(brand.slug), execution.controls(brand.slug),
    ])
    return { brand, readiness, console, controls }
  })), [active.map((brand) => brand.slug).join('|')])

  const mutate = async (key: string, action: () => Promise<unknown>, success: string) => {
    setBusy(key)
    try { await action(); notify(success); load.reload() }
    catch (error) { notify(describe(error), 'bad') }
    finally { setBusy('') }
  }
  const configure = (brand: Brand) => {
    const id = agentId.trim()
    if (!id) return notify('Enter an agent ID first.', 'bad')
    void mutate(`configure:${brand.id}`, () => api.configure(brand.slug, id, channel, true),
      `${id} is enabled. Start it, then record a heartbeat before claiming work.`)
  }
  const totals = useMemo(() => load.data ? {
    code: Math.round(load.data.reduce((sum, item) => sum + item.readiness.summary.code_ready_percent, 0) / load.data.length),
    live: Math.round(load.data.reduce((sum, item) => sum + item.readiness.summary.live_ready_percent, 0) / load.data.length),
    readyAgents: load.data.reduce((sum, item) => sum + item.console.execution_agents.filter((agent) => agent.ready_to_claim).length, 0),
  } : null, [load.data])

  return (
    <Shell title="Agents & live readiness" crumb={selected ? 'Brand operations' : 'Workspace operations'}>
      {load.error && <ErrorState message={load.error} retry={load.reload} />}
      {load.loading && !load.data && <Loading />}
      {load.data && totals && <>
        <div className="grid" style={{ gridTemplateColumns: 'repeat(3, minmax(0, 1fr))' }}>
          <Summary label="Code readiness" value={`${totals.code}%`} pct={totals.code} note="Product capabilities implemented" />
          <Summary label="Provider proof" value={`${totals.live}%`} pct={totals.live} note="Required live evidence present" />
          <Summary label="Agents ready to claim" value={totals.readyAgents} pct={totals.readyAgents ? 100 : 0} note="Heartbeat fresh within 15 minutes" />
        </div>
        {load.data.map((bundle) => <BrandAgents key={bundle.brand.id} bundle={bundle} busy={busy}
          agentId={agentId} channel={channel} setAgentId={setAgentId} setChannel={setChannel}
          configure={() => configure(bundle.brand)} mutate={mutate} />)}
      </>}
    </Shell>
  )
}

function Summary({ label, value, pct, note }: { label: string; value: string | number; pct: number; note: string }) {
  return <Card className="kpi"><div className="lbl">{label}</div><div className="val">{value}</div><Bar pct={pct} /><div className="meta">{note}</div></Card>
}

function BrandAgents({ bundle, busy, agentId, channel, setAgentId, setChannel, configure, mutate }: {
  bundle: AgentBundle; busy: string; agentId: string; channel: 'browser' | 'mcp'
  setAgentId: (value: string) => void; setChannel: (value: 'browser' | 'mcp') => void
  configure: () => void
  mutate: (key: string, action: () => Promise<unknown>, success: string) => Promise<void>
}) {
  const { brand, readiness, console, controls } = bundle
  const recovery = recoveryItems(console)
  const codeChecks = readiness.checks.filter((check) => check.code_ready)
  const proofChecks = readiness.checks.filter((check) => check.required_for_live !== false)
  return <div className="stack" style={{ gap: 16, marginTop: 16 }}>
    <div className="row" style={{ justifyContent: 'space-between' }}><BrandTag brand={brand} />
      <Chip kind={readiness.ready ? 'ok' : 'human'}>{readiness.ready ? 'live ready' : 'proof incomplete'}</Chip></div>
    <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', alignItems: 'start' }}>
      <ReadinessCard title="Code readiness" subtitle="Built capability, independent of account state" checks={codeChecks} mode="code" />
      <ReadinessCard title="Provider proof" subtitle="Accounts, health, receipts, and scheduler evidence" checks={proofChecks} mode="proof" />
    </div>
    <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 1.25fr) minmax(320px, .75fr)', alignItems: 'start' }}>
      <Card>
        <CardHeader icon="bot" iconColor="var(--accent)" title="Execution agents" sub="browser or MCP helper identity" />
        {console.execution_agents.length === 0 ? <Empty>No execution agent is configured.</Empty> : console.execution_agents.map((agent) =>
          <AgentRow key={agent.agent_id} brand={brand} agent={agent} busy={busy} mutate={mutate} />)}
        <div className="card-b stack" style={{ gap: 8, borderTop: '1px solid var(--line-2)' }}>
          <div className="meta">Configure an identity. This grants no approval and performs no provider action.</div>
          <div className="row">
            <input className="input" aria-label={`Agent ID for ${brand.name}`} value={agentId} maxLength={200} onChange={(event) => setAgentId(event.target.value)} />
            <select className="input" aria-label={`Agent channel for ${brand.name}`} value={channel} onChange={(event) => setChannel(event.target.value as 'browser' | 'mcp')}>
              <option value="browser">Browser</option><option value="mcp">MCP</option>
            </select>
            <button className="btn" disabled={busy === `configure:${brand.id}`} onClick={configure}>Configure & enable</button>
          </div>
        </div>
      </Card>
      <Card>
        <CardHeader icon="lock" title="Provider controls" sub="new claims only" />
        <div className="card-b stack" style={{ gap: 10 }}>
          {controls.controls.map((control) => <div className="row" key={control.provider} style={{ justifyContent: 'space-between' }}>
            <div><b>{control.provider === 'all' ? 'All assisted execution' : control.provider.toUpperCase()}</b>
              <div className="meta">{control.effective_enabled ? 'New claims allowed' : 'New claims blocked'}</div></div>
            <button className={`btn sm ${control.enabled ? 'danger' : 'ok'}`} disabled={busy === `control:${brand.id}:${control.provider}`}
              onClick={() => {
                if (control.enabled && !window.confirm(`Disable ${control.provider} assisted execution for ${brand.name}? Existing claims are not cancelled.`)) return
                void mutate(`control:${brand.id}:${control.provider}`, () => execution.setControl(brand.slug, control.provider, !control.enabled),
                  `${titleCase(control.provider)} assisted execution ${control.enabled ? 'disabled' : 'enabled'}.`)
              }}>{control.enabled ? 'Disable' : 'Enable'}</button>
          </div>)}
          <div className="meta">Controls stop future claims. They do not revoke approvals, cancel active claims, or repeat provider actions.</div>
        </div>
      </Card>
    </div>
    <Card>
      <CardHeader icon="alert" iconColor={recovery.some((item) => item.urgent) ? 'var(--human)' : 'var(--muted)'} title="Recovery guidance" sub="safe next actions from the execution ledger" />
      {recovery.length === 0 ? <Empty>No handoff recovery is required.</Empty> : recovery.map((item) =>
        <div className="list-item" key={item.key}><div className="row"><Chip kind={item.urgent ? 'human' : 'info'}>{item.provider}</Chip><b>{item.title}</b></div><div className="muted">{item.text}</div></div>)}
      <div className="card-b meta" style={{ borderTop: '1px solid var(--line-2)' }}>A “do not repeat” instruction means the provider action may already exist. Reconcile the existing result with the original claim instead of starting again.</div>
    </Card>
  </div>
}

function ReadinessCard({ title, subtitle, checks, mode }: { title: string; subtitle: string; checks: LiveReadiness['checks']; mode: 'code' | 'proof' }) {
  return <Card><CardHeader icon={mode === 'code' ? 'layers' : 'checkcircle'} title={title} sub={subtitle} />
    <div className="card-b stack" style={{ gap: 10 }}>{checks.map((check) => {
      const ready = mode === 'code' ? check.code_ready : check.status === 'ready'
      return <div key={check.id} className="check-row" style={{ alignItems: 'flex-start' }}><Icon name={ready ? 'check' : 'alert'} size={15} style={{ color: ready ? 'var(--ok)' : 'var(--human)' }} />
        <div><div className="row"><b>{check.label}</b><Chip kind={ready ? 'ok' : 'human'}>{ready ? 'ready' : mode === 'code' ? 'not implemented' : 'needs proof'}</Chip></div>
          <div className="meta">{check.detail}</div>{mode === 'proof' && !ready && check.actions.map((action) => <div className="meta" key={action}>Next: {action}</div>)}</div></div>
    })}</div></Card>
}

function AgentRow({ brand, agent, busy, mutate }: { brand: Brand; agent: ExecutionAgent; busy: string; mutate: (key: string, action: () => Promise<unknown>, success: string) => Promise<void> }) {
  const key = `${brand.id}:${agent.agent_id}`
  return <div className="list-item"><div className="row" style={{ justifyContent: 'space-between' }}><div><b className="mono">{agent.agent_id}</b><div className="meta">{agent.channel.toUpperCase()} · {agent.last_heartbeat_at ? `heartbeat ${relTime(agent.last_heartbeat_at)}` : 'no heartbeat recorded'}</div></div>
    <Chip kind={agent.ready_to_claim ? 'ok' : agent.enabled ? 'human' : 'neutral'}>{agent.ready_to_claim ? 'ready to claim' : agent.enabled ? 'heartbeat needed' : 'disabled'}</Chip></div>
    <div className="row" style={{ marginTop: 8 }}><button className="btn sm" disabled={!agent.enabled || busy === `heartbeat:${key}`} onClick={() => void mutate(`heartbeat:${key}`, () => api.heartbeat(brand.slug, agent.agent_id), `Heartbeat recorded for ${agent.agent_id}.`)}><Icon name="refresh" size={13} />Heartbeat</button>
      <button className="btn sm" disabled={busy === `toggle:${key}`} onClick={() => void mutate(`toggle:${key}`, () => api.configure(brand.slug, agent.agent_id, agent.channel as 'browser' | 'mcp', !agent.enabled), `${agent.agent_id} ${agent.enabled ? 'disabled' : 'enabled'}.`)}>{agent.enabled ? 'Disable' : 'Enable'}</button></div></div>
}
