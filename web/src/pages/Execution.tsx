import { useMemo, useState } from 'react'
import { execution } from '../api/endpoints'
import type { ExecutionAudit, ExecutionConsole, ExecutionTask } from '../api/types'
import { describe, useLoad } from '../api/useLoad'
import { ChannelIcon, Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { Card, CardHeader, Chip, Empty, ErrorState, KV, Loading, StatusChip } from '../components/ui'
import { relTime, titleCase } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'

interface Capability { token: string; actor: string }

export default function Execution() {
  const { selected } = useBrands()
  const toast = useToast()
  const state = useLoad(async () => {
    if (!selected) return null
    const [consoleData, controls] = await Promise.all([
      execution.console(selected), execution.controls(selected),
    ])
    return { consoleData, controls: controls.controls }
  }, [selected])
  const [capabilities, setCapabilities] = useState<Record<string, Capability>>({})

  const mutate = async (work: () => Promise<unknown>, success: string) => {
    try {
      await work()
      toast.notify(success)
      state.reload()
    } catch (error) {
      toast.notify(describe(error), 'bad')
    }
  }

  if (!selected) return <Shell title="Execution"><Empty>Select one brand to open its governed execution console.</Empty></Shell>
  return (
    <Shell title="Execution" right={<Chip kind="human" icon="lock">no generic publish control</Chip>}>
      {state.loading && !state.data && <Loading />}
      {state.error && <ErrorState message={state.error} retry={state.reload} />}
      {state.data && (
        <>
          <Safety />
          <Card>
            <CardHeader icon="lock" title="Provider boundaries" sub="Stops new claims; it never grants approval or performs a provider action." />
            <div className="card-b row" style={{ flexWrap: 'wrap' }}>
              {state.data.controls.map((control) => (
                <button
                  key={control.provider}
                  className={`btn sm ${control.effective_enabled ? '' : 'danger'}`}
                  onClick={() => mutate(
                    () => execution.setControl(selected, control.provider, !control.enabled),
                    `${titleCase(control.provider)} execution ${control.enabled ? 'paused' : 'enabled'}`,
                  )}
                >
                  <Icon name={control.effective_enabled ? 'checkcircle' : 'alert'} size={14} />
                  {titleCase(control.provider)}: {control.effective_enabled ? 'enabled' : 'paused'}
                </button>
              ))}
            </div>
          </Card>
          <Card>
            <CardHeader icon="send" title="Exact-approved tasks" sub={`${state.data.consoleData.tasks.length} governed handoff${state.data.consoleData.tasks.length === 1 ? '' : 's'}`} />
            {state.data.consoleData.tasks.length === 0 ? <Empty>No exact-approved provider actions are waiting.</Empty> : (
              <div className="stack" style={{ padding: 12 }}>
                {state.data.consoleData.tasks.map((task) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    consoleData={state.data!.consoleData}
                    capability={capabilities[task.id]}
                    setCapability={(value) => setCapabilities((current) => ({ ...current, [task.id]: value }))}
                    mutate={mutate}
                  />
                ))}
              </div>
            )}
          </Card>
          <Card>
            <CardHeader icon="bot" title="Fresh execution agents" sub="Only enabled agents with a recent heartbeat can claim work." />
            {state.data.consoleData.execution_agents.length === 0 ? <Empty>Configure an execution agent from the Agents page first.</Empty> : (
              <table><thead><tr><th>Agent</th><th>Channel</th><th>Heartbeat</th><th>Claim state</th></tr></thead><tbody>
                {state.data.consoleData.execution_agents.map((agent) => <tr key={agent.agent_id}>
                  <td className="mono">{agent.agent_id}</td><td>{titleCase(agent.channel)}</td>
                  <td>{agent.last_heartbeat_at ? relTime(agent.last_heartbeat_at) : 'never'}</td>
                  <td><Chip kind={agent.ready_to_claim ? 'ok' : 'human'}>{agent.ready_to_claim ? 'fresh' : 'not ready'}</Chip></td>
                </tr>)}
              </tbody></table>
            )}
          </Card>
        </>
      )}
    </Shell>
  )
}

function Safety() {
  return <div className="ai-note"><Icon name="lock" size={16} /><div><strong>Assisted execution only.</strong> This console prepares and reconciles exact-approved work. Loading this page performs no provider write, claims never grant approval, and Beehiiv pulls contain aggregate data only.</div></div>
}

function TaskCard({ task, consoleData, capability, setCapability, mutate }: {
  task: ExecutionTask
  consoleData: ExecutionConsole
  capability?: Capability
  setCapability: (value: Capability) => void
  mutate: (work: () => Promise<unknown>, success: string) => Promise<void>
}) {
  const [destination, setDestination] = useState(task.connector_account_id ?? '')
  const [agent, setAgent] = useState('')
  const [assetPath, setAssetPath] = useState('')
  const [manifest, setManifest] = useState<Record<string, unknown> | null>(null)
  const [receiptId, setReceiptId] = useState('')
  const [receiptUrl, setReceiptUrl] = useState('')
  const [audit, setAudit] = useState<ExecutionAudit[] | null>(null)
  const freshAgents = useMemo(() => consoleData.execution_agents.filter((item) => item.enabled && item.ready_to_claim && (item.channel === task.provider || item.channel === 'all')), [consoleData, task.provider])
  const material = task.execution_payload.body ?? task.execution_payload.text ?? task.execution_payload.subject ?? task.execution_payload
  const canBegin = task.status === 'claimed' && capability && (task.provider !== 'x' || task.confirmation_current)

  const claim = async () => {
    const result = await execution.claim(task.id, agent)
    setCapability({ token: result.claim_token, actor: agent })
  }

  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div className="card-h" style={{ flexWrap: 'wrap' }}>
        <ChannelIcon channel={task.provider} />
        <span>{task.provider === 'beehiiv' ? 'Beehiiv private draft' : 'X public post'}</span>
        <span className="mono">revision {task.revision}</span>
        <StatusChip status={task.status} />
        <span style={{ marginLeft: 'auto' }}><Chip kind={task.action_boundary === 'provider_action_started' ? 'human' : task.action_boundary === 'completed' ? 'ok' : 'neutral'}>{task.action_boundary.replaceAll('_', ' ')}</Chip></span>
      </div>
      <div className="card-b stack" style={{ gap: 14 }}>
        <KV rows={[
          { k: 'Resource', v: <span className="mono">{task.resource_type} · {task.resource_id}</span> },
          { k: 'Fingerprint', v: <span className="mono">{task.material_fingerprint}</span> },
          { k: 'Exact material', v: typeof material === 'string' ? material : <pre className="payload-preview">{JSON.stringify(material, null, 2)}</pre> },
          { k: 'Next safe step', v: task.operator_next_action.text },
        ]} />

        {task.destination_binding_required && <div className="row" style={{ alignItems: 'end', flexWrap: 'wrap' }}>
          <div className="field" style={{ minWidth: 260 }}><label htmlFor={`destination-${task.id}`}>Exact destination account</label>
            <select id={`destination-${task.id}`} className="input" value={destination} onChange={(event) => setDestination(event.target.value)}>
              <option value="">Select a same-brand account</option>
              {task.destination_options.map((option) => <option key={option.id} value={option.id}>{option.display_name ?? option.username ?? option.account_key ?? option.id}</option>)}
            </select>
          </div>
          <button className="btn" disabled={!destination} onClick={() => mutate(() => execution.bindDestination(task.id, destination), 'Destination bound to this exact task')}>Bind destination</button>
        </div>}

        {task.status === 'pending' && !task.destination_binding_required && <div className="row" style={{ alignItems: 'end', flexWrap: 'wrap' }}>
          <div className="field" style={{ minWidth: 260 }}><label htmlFor={`agent-${task.id}`}>Fresh execution agent</label>
            <select id={`agent-${task.id}`} className="input" value={agent} onChange={(event) => setAgent(event.target.value)}>
              <option value="">Select an agent</option>
              {freshAgents.map((item) => <option key={item.agent_id} value={item.agent_id}>{item.agent_id}</option>)}
            </select>
          </div>
          <button className="btn" disabled={!agent} onClick={() => mutate(claim, 'Exact task claimed; its one-time capability stays only in this tab')}>Claim exact task</button>
          {freshAgents.length === 0 && <span className="meta">No matching agent has a fresh heartbeat.</span>}
        </div>}

        {task.provider === 'beehiiv' && ['pending', 'claimed'].includes(task.status) && <div className="stack">
          <div className="row" style={{ alignItems: 'end', flexWrap: 'wrap' }}>
            <div className="field" style={{ minWidth: 320, flex: 1 }}><label htmlFor={`asset-${task.id}`}>Approved media file or URL</label><input id={`asset-${task.id}`} className="input" value={assetPath} onChange={(event) => setAssetPath(event.target.value)} placeholder="/absolute/path/image.png or https://…" /></div>
            <button className="btn" disabled={!assetPath} onClick={() => mutate(async () => setManifest(await execution.beehiivManifest(task.id, assetPath)), 'Private-draft instructions prepared')}>Prepare private-draft instructions</button>
          </div>
          {manifest && <details><summary>Review exact Beehiiv manifest</summary><pre className="payload-preview">{JSON.stringify(manifest, null, 2)}</pre></details>}
        </div>}

        {task.provider === 'x' && task.status === 'claimed' && !task.confirmation_current && <button className="btn danger" onClick={() => mutate(() => execution.confirmX(task.id, task.revision, task.material_fingerprint), 'Exact X action confirmed for up to five minutes')}><Icon name="alert" size={14} />Confirm this exact X action for 5 minutes</button>}

        {task.status === 'claimed' && !capability && <div className="ai-note"><Icon name="alert" size={16} /><div>This task was claimed elsewhere. Continue in the original helper; this page will not recreate or reveal its one-time capability.</div></div>}
        {canBegin && !task.external_action_started_at && <button className="btn danger" onClick={() => mutate(() => execution.begin(task.id, capability.actor, capability.token), 'Provider-action boundary recorded')}><Icon name="alert" size={14} />Mark exact provider action begun</button>}

        {capability && (task.status === 'claimed' || task.status === 'needs_attention') && <div className="stack">
          <div className="ai-note"><Icon name="history" size={16} /><div>Record a receipt only after verifying the existing provider result. If the boundary shows “provider action started,” do not repeat the provider action.</div></div>
          <div className="row" style={{ alignItems: 'end', flexWrap: 'wrap' }}>
            <div className="field"><label htmlFor={`receipt-id-${task.id}`}>{task.receipt_requirements.external_id}</label><input id={`receipt-id-${task.id}`} className="input" value={receiptId} onChange={(event) => setReceiptId(event.target.value)} /></div>
            <div className="field" style={{ minWidth: 300, flex: 1 }}><label htmlFor={`receipt-url-${task.id}`}>{task.receipt_requirements.external_url}</label><input id={`receipt-url-${task.id}`} className="input" value={receiptUrl} onChange={(event) => setReceiptUrl(event.target.value)} /></div>
            <button className="btn" disabled={!receiptId || !receiptUrl || !task.external_action_started_at} onClick={() => mutate(() => execution.receipt(task.id, { claim_token: capability.token, external_id: receiptId, external_url: receiptUrl, status: task.receipt_requirements.status, content_fingerprint: task.material_fingerprint }), 'Existing provider result reconciled')}>Record existing receipt</button>
          </div>
        </div>}

        {task.status === 'completed' && <KV rows={[{ k: 'Provider ID', v: task.receipt_external_id }, { k: 'Provider URL', v: task.receipt_external_url ? <a href={task.receipt_external_url} target="_blank" rel="noreferrer">Open provider result</a> : '—' }, { k: 'Recorded', v: task.receipt_recorded_at ? relTime(task.receipt_recorded_at) : '—' }]} />}

        <div><button className="btn ghost sm" onClick={() => execution.audit(task.id).then(setAudit).catch((error) => mutate(() => Promise.reject(error), ''))}><Icon name="history" size={14} />{audit ? 'Refresh audit' : 'Show audit'}</button></div>
        {audit && <div className="audit-list">{audit.length === 0 ? <Empty>No audit entries.</Empty> : audit.map((entry) => <div className="feed-item" key={entry.sequence}><div><strong>{titleCase(entry.action)}</strong><div className="meta">{entry.actor} · {relTime(entry.at)}</div></div><pre className="payload-preview">{JSON.stringify(entry.detail, null, 2)}</pre></div>)}</div>}
      </div>
    </div>
  )
}
