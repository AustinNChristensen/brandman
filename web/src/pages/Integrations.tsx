import { useMemo, useState, type FormEvent } from 'react'
import { integrations } from '../api/endpoints'
import type { ConnectionLane, ConnectorAccount, ConnectorHealthCheck, LiveReadiness } from '../api/types'
import { describe, useLoad } from '../api/useLoad'
import { ChannelIcon, Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { Bar, Card, CardHeader, Chip, Empty, ErrorState, Loading, Modal } from '../components/ui'
import { relTime, titleCase } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'

type Bundle = Awaited<ReturnType<typeof loadBundle>>
type Setup = { lane: ConnectionLane; account?: ConnectorAccount }

async function loadBundle(slug: string) {
  const onboarding = await integrations.onboarding(slug)
  const [readiness, connectors, connections, health] = await Promise.all([
    integrations.readiness(slug), integrations.connectors(slug),
    onboarding.encryption.ready ? integrations.connections(slug) : Promise.resolve([]),
    integrations.health(slug),
  ])
  return { onboarding, readiness, connectors, connections, health }
}

const tone = (status: string): 'ok' | 'bad' | 'human' | 'info' | 'neutral' =>
  ['ready', 'healthy', 'connected'].includes(status) ? 'ok'
    : ['queued', 'running'].includes(status) ? 'info'
      : ['not_configured', 'disconnected'].includes(status) ? 'neutral'
        : ['reconnect_required', 'unhealthy', 'failed', 'timed_out'].includes(status) ? 'bad' : 'human'

const allLanes = (bundle: Bundle) => Object.values(bundle.onboarding.providers).flatMap((provider) => provider.lanes)
const laneFor = (account: ConnectorAccount, bundle: Bundle): ConnectionLane | undefined => {
  const declared = allLanes(bundle).find((lane) => lane.lane === String(account.configuration?.connection_role ?? ''))
  if (declared) return declared
  if (account.connector_type === 'website') return {
    lane: 'website', provider: 'website', title: 'Website analytics',
    purpose: 'Read authenticated aggregate product and conversion events.',
    scopes: account.scopes, capabilities: account.capabilities,
    credential_inputs: [{ key: 'access_token', label: 'Access token', secret: true }],
    can_read: true, can_write: false, write_boundary: 'Analytics ingestion only; cannot deploy or change the website.',
  }
}

export default function Integrations() {
  const { selected, active } = useBrands()
  const brand = selected ? active[0] : undefined
  const load = useLoad(() => brand ? loadBundle(brand.slug) : Promise.resolve(null), [brand?.slug])
  const { notify } = useToast()
  const [setup, setSetup] = useState<Setup | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const act = async (key: string, action: () => Promise<unknown>, message: string) => {
    setBusy(key)
    try { await action(); notify(message); load.reload() }
    catch (error) { notify(describe(error), 'bad') }
    finally { setBusy(null) }
  }
  return <Shell title="Integrations" crumb={brand?.name} right={<Chip kind="agent" icon="lock">secrets are write-only</Chip>}>
    {!selected && <Card><Empty>Select one brand to review or change its connections. Integration changes are always brand-specific.</Empty></Card>}
    {selected && load.loading && !load.data && <Loading label="Checking connection readiness…" />}
    {load.error && <ErrorState message={load.error} retry={load.reload} />}
    {brand && load.data && <>
      <ReadinessSummary readiness={load.data.readiness} />
      <ModeComparison bundle={load.data} />
      <Connections bundle={load.data} busy={busy} onSetup={setSetup}
        onHealth={(account) => act(`health:${account.id}`, () => integrations.checkHealth(brand.slug, account.id), 'Read-only health check queued.')}
        onDisconnect={(account) => { if (window.confirm(`Disconnect ${account.display_name}? Scheduled API work for this lane will stop until it is reconnected.`)) void act(`disconnect:${account.id}`, () => integrations.disconnect(brand.slug, account.connector_type, account.account_key), 'Connection disconnected. Stored credentials are no longer usable.') }} />
      <AvailableLanes bundle={load.data} onSetup={(lane) => setSetup({ lane })} />
      <Recovery readiness={load.data.readiness} />
      {setup && <SetupModal slug={brand.slug} setup={setup} encryptionReady={load.data.onboarding.encryption.ready} onClose={() => setSetup(null)} onDone={() => { setSetup(null); load.reload() }} />}
    </>}
  </Shell>
}

function ReadinessSummary({ readiness }: { readiness: LiveReadiness }) {
  const remaining = readiness.summary.required_checks_total - readiness.summary.required_checks_ready
  return <div className="grid integrations-kpis">
    <Card className="kpi"><div className="lbl">Live readiness</div><div className="val">{readiness.summary.live_ready_percent}%</div><Bar pct={readiness.summary.live_ready_percent} color={readiness.ready ? 'var(--ok)' : 'var(--human)'} /></Card>
    <Card className="kpi"><div className="lbl">Required work remaining</div><div className="val">{remaining}</div><div className="meta">{remaining ? `${readiness.summary.required_checks_ready} of ${readiness.summary.required_checks_total} required checks ready` : 'All required checks are ready'}</div></Card>
    <Card className="kpi"><div className="lbl">Standalone lanes ready</div><div className="val">{readiness.summary.connector_accounts_ready}/{readiness.summary.connector_accounts_total}</div><div className="meta">Saved setup and provider health are checked separately.</div></Card>
  </div>
}

function ModeComparison({ bundle }: { bundle: Bundle }) {
  return <Card><CardHeader icon="layers" title="Choose how Brand OS connects" sub="start assisted, then graduate individual lanes when API access is available" />
    <div className="grid card-b integrations-two-col">{bundle.onboarding.modes.map((mode) => <div key={mode.mode} className="integration-box stack">
      <div className="row"><b>{mode.title}</b><Chip kind={mode.available_now ? 'ok' : 'neutral'}>{mode.available_now ? 'available' : 'admin setup needed'}</Chip></div>
      <div>{mode.best_for}</div><div className="meta">{mode.tradeoff}</div>
      <div className="check-row"><Icon name={mode.credentials_stored ? 'lock' : 'checkcircle'} size={15} /><span>{mode.credentials_stored ? 'Credentials are encrypted and never displayed after entry.' : 'No provider credentials are stored.'}</span></div>
    </div>)}</div>
    {!bundle.onboarding.encryption.ready && <div className="ai-note integration-alert"><Icon name="alert" size={16} /><div><b>Standalone setup is locked.</b><div>{bundle.onboarding.encryption.operator_action}</div></div></div>}
  </Card>
}

function Connections({ bundle, busy, onSetup, onHealth, onDisconnect }: { bundle: Bundle; busy: string | null; onSetup: (setup: Setup) => void; onHealth: (account: ConnectorAccount) => void; onDisconnect: (account: ConnectorAccount) => void }) {
  const connectionByKey = useMemo(() => new Map(bundle.connections.map((item) => [`${item.provider}:${item.account_id}`, item])), [bundle.connections])
  const healthByAccount = useMemo(() => {
    const result = new Map<string, ConnectorHealthCheck>()
    for (const item of bundle.health) if (!result.has(item.connector_account_id)) result.set(item.connector_account_id, item)
    return result
  }, [bundle.health])
  return <Card><CardHeader icon="plug" title="Your connections" sub="metadata, least-privilege access, and latest read-only health" />
    {!bundle.connectors.length ? <Empty>No lanes are prepared yet. Choose a least-privilege lane below.</Empty> : bundle.connectors.map((account) => {
      const connection = connectionByKey.get(`${account.connector_type}:${account.account_key}`)
      const health = healthByAccount.get(account.id)
      const lane = laneFor(account, bundle)
      const assisted = ['browser_assisted', 'mcp_assisted'].includes(String(account.configuration?.delivery_mode ?? ''))
      const scopes = connection?.required_scopes ?? account.scopes
      return <div key={account.id} className="card-b integration-row">
        <div className="row integration-heading"><div className="row"><ChannelIcon channel={account.connector_type} size={18} /><div><b>{account.display_name}</b><div className="meta">{lane?.title ?? titleCase(account.connector_type)} · {assisted ? 'browser-assisted' : 'standalone API'} · <span className="mono">{account.account_key}</span></div></div></div><div className="row"><Chip kind={tone(connection?.status ?? account.status)}>{connection?.status ?? account.status}</Chip>{health && <Chip kind={tone(health.status)}>health: {health.status}</Chip>}</div></div>
        <div className="integration-detail"><div><div className="meta">Allowed access</div><div className="integration-scopes">{scopes.length ? scopes.map((scope) => <Chip key={scope} kind="neutral">{scope}</Chip>) : <span className="meta">No API scopes — assisted/public source</span>}</div>{connection?.last_error_code && <div className="meta integration-error">Last error: {connection.last_error_code}</div>}{health?.requested_at && <div className="meta">Last health request {relTime(health.requested_at)}</div>}</div>
          <div className="row integration-actions">{!assisted && lane && <button className="btn sm" onClick={() => onSetup({ lane, account })}>{connection?.reconnect_required ? 'Reconnect' : connection ? 'Rotate credential' : 'Connect'}</button>}{!assisted ? <button className="btn sm" disabled={busy === `health:${account.id}`} onClick={() => onHealth(account)}><Icon name="refresh" size={13} />Check health</button> : <Chip kind="info">health: helper-managed</Chip>}{connection && connection.status !== 'disconnected' && <button className="btn sm danger" disabled={busy === `disconnect:${account.id}`} onClick={() => onDisconnect(account)}>Disconnect</button>}</div></div>
      </div>
    })}
  </Card>
}

function AvailableLanes({ bundle, onSetup }: { bundle: Bundle; onSetup: (lane: ConnectionLane) => void }) {
  const existing = new Set(bundle.connectors.map((account) => String(account.configuration?.connection_role ?? '')))
  const lanes = allLanes(bundle).filter((lane) => !existing.has(lane.lane))
  return <Card><CardHeader icon="plus" title="Add a standalone lane" sub="read and write permissions stay separate" />{!lanes.length ? <Empty>Every supported standalone lane is prepared.</Empty> : <div className="grid card-b integrations-two-col">{lanes.map((lane) => <div key={lane.lane} className="integration-box stack"><div className="row"><ChannelIcon channel={lane.provider} /><b>{lane.title}</b><Chip kind={lane.can_write ? 'human' : 'info'}>{lane.can_write ? 'approved writes' : 'read only'}</Chip></div><div>{lane.purpose}</div><div className="meta">{lane.write_boundary}</div><div className="integration-scopes">{lane.scopes.map((scope) => <Chip key={scope} kind="neutral">{scope}</Chip>)}</div><button className="btn sm" onClick={() => onSetup(lane)}>Prepare this lane</button></div>)}</div>}</Card>
}

function Recovery({ readiness }: { readiness: LiveReadiness }) {
  const gaps = readiness.checks.filter((check) => check.status !== 'ready' && check.actions.length)
  return <Card><CardHeader icon="alert" title="Readiness gaps" sub="the next supported recovery actions from Brand OS" />{!gaps.length ? <Empty>There are no reported recovery actions.</Empty> : gaps.map((check) => <div className="card-b integration-row" key={check.id}><div className="row"><b>{check.label}</b><Chip kind={tone(check.status)}>{check.status.replace(/_/g, ' ')}</Chip>{check.required_for_live === false && <Chip kind="neutral">optional for live</Chip>}</div><div className="meta integration-detail-copy">{check.detail}</div><ol className="integration-recovery">{check.actions.map((action) => <li key={action}>{action}</li>)}</ol></div>)}</Card>
}

function SetupModal({ slug, setup, encryptionReady, onClose, onDone }: { slug: string; setup: Setup; encryptionReady: boolean; onClose: () => void; onDone: () => void }) {
  const { notify } = useToast()
  const [accountKey, setAccountKey] = useState(setup.account?.account_key ?? '')
  const [displayName, setDisplayName] = useState(setup.account?.display_name ?? setup.lane.title)
  const [secrets, setSecrets] = useState<Record<string, string>>({})
  const [prepared, setPrepared] = useState(Boolean(setup.account))
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true)
    try {
      if (!prepared) { await integrations.prepareLane(slug, setup.lane, accountKey.trim(), displayName.trim()); setPrepared(true); notify('Least-privilege lane prepared. Enter its credential to finish.'); return }
      const credentialValues = { ...secrets }
      if (credentialValues.expires_at) credentialValues.expires_at = new Date(credentialValues.expires_at).toISOString()
      await integrations.connect(slug, setup.lane, accountKey.trim(), displayName.trim(), credentialValues)
      setSecrets({}); notify('Connection saved. Secret values were not returned or displayed.'); onDone()
    } catch (error) { notify(describe(error), 'bad') }
    finally { setBusy(false) }
  }
  return <Modal title={`${setup.account ? 'Reconnect' : 'Set up'} ${setup.lane.title}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button form="connection-setup" className="btn primary" disabled={busy || !encryptionReady}>{prepared ? 'Save credential' : 'Prepare lane'}</button></>}>
    <form id="connection-setup" className="stack" onSubmit={submit}>
      <div className="ai-note"><Icon name="lock" size={16} /><div><b>Secret values are write-only.</b><div>Brand OS encrypts them on receipt. This page cannot retrieve or show them later.</div></div></div>
      <div className="field"><label htmlFor="connection-id">Connection ID</label><input id="connection-id" className="input" required disabled={prepared} value={accountKey} onChange={(event) => setAccountKey(event.target.value)} placeholder={setup.lane.provider === 'beehiiv' ? 'publication ID' : 'unique account lane ID'} /></div>
      <div className="field"><label htmlFor="connection-name">Display name</label><input id="connection-name" className="input" required value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></div>
      <div><div className="meta">Exact allowed scopes</div><div className="integration-scopes">{setup.lane.scopes.map((scope) => <Chip key={scope} kind="neutral">{scope}</Chip>)}</div><div className="meta integration-detail-copy">{setup.lane.write_boundary}</div></div>
      {!encryptionReady && <div className="state error">Credential encryption must be configured by an administrator before standalone credentials can be entered.</div>}
      {prepared && setup.lane.credential_inputs.map((input) => <div className="field" key={input.key}><label htmlFor={`credential-${input.key}`}>{input.label}</label><input id={`credential-${input.key}`} className="input" required type={input.secret ? 'password' : input.key === 'expires_at' ? 'datetime-local' : 'text'} autoComplete="new-password" value={secrets[input.key] ?? ''} onChange={(event) => setSecrets((current) => ({ ...current, [input.key]: event.target.value }))} /></div>)}
    </form>
  </Modal>
}
