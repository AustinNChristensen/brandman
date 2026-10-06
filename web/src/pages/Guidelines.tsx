import { useState } from 'react'
import { guidelines as api } from '../api/endpoints'
import type { BrandGuideline, BrandGuidelineAudit, BrandGuidelineVersion } from '../api/types'
import { describe, useLoad } from '../api/useLoad'
import { Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { Card, CardHeader, Chip, Empty, ErrorState, Loading, Modal, StatusChip } from '../components/ui'
import { relTime, titleCase } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'

type Bundle = { guidelines: BrandGuideline[]; audits: Record<string, BrandGuidelineAudit[]> }
type Dialog = 'create' | 'version' | 'activate' | 'archive' | null

async function loadGuidelines(slug: string): Promise<Bundle> {
  const items = await api.list(slug, true)
  const audits = Object.fromEntries(await Promise.all(items.map(async (item) => [item.id, await api.audit(item.id)] as const)))
  return { guidelines: items, audits }
}

export default function Guidelines() {
  const { selected, active } = useBrands()
  const brand = selected ? active[0] : undefined
  const load = useLoad(() => brand ? loadGuidelines(brand.slug) : Promise.resolve(null), [brand?.slug])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [activationVersion, setActivationVersion] = useState<number | null>(null)
  const [dialog, setDialog] = useState<Dialog>(null)
  const [busy, setBusy] = useState(false)
  const { notify } = useToast()
  const selectedGuideline = load.data?.guidelines.find((item) => item.id === selectedId)
    ?? load.data?.guidelines.find((item) => item.status === 'active') ?? load.data?.guidelines[0]

  const mutate = async (message: string, fn: () => Promise<unknown>) => {
    setBusy(true)
    try { await fn(); notify(message); setDialog(null); load.reload() }
    catch (error) { notify(describe(error), 'bad') }
    finally { setBusy(false) }
  }

  if (!selected) return <Shell title="Guidelines"><Card><Empty>Select one brand to manage its governed instructions.</Empty></Card></Shell>
  return <Shell title="Guidelines" crumb={brand?.name} right={<Chip kind="agent" icon="lock">immutable versions · human activation</Chip>}>
    {load.error && <ErrorState message={load.error} retry={load.reload} />}
    {load.loading && !load.data && <Loading label="Loading governed instructions…" />}
    {load.data && <>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div className="meta">Generation resolves only the exact active instruction version for this brand, content type, and channel.</div>
        <button className="btn primary" onClick={() => setDialog('create')}><Icon name="plus" size={14} />New instruction scope</button>
      </div>
      <div className="campaign-layout">
        <Card className="campaign-list">
          <CardHeader icon="book" title="Instruction scopes" sub={`${load.data.guidelines.length}`} />
          {!load.data.guidelines.length ? <Empty>No governed instructions exist for this brand.</Empty> : load.data.guidelines.map((item) =>
            <button key={item.id} className={`list-item ${selectedGuideline?.id === item.id ? 'on' : ''}`} onClick={() => setSelectedId(item.id)}>
              <div className="row" style={{ justifyContent: 'space-between' }}><b>{item.name}</b><StatusChip status={item.status} /></div>
              <div className="meta">{item.content_type} · {item.channel}</div>
              <div className="meta">{item.active_version ? `active v${item.active_version.version}` : 'no active version'}</div>
            </button>)}
        </Card>
        <div className="stack" style={{ gap: 12 }}>
          {!selectedGuideline ? <Card><Empty>Create an instruction scope to begin.</Empty></Card> : <>
            <ActiveInstruction guideline={selectedGuideline} />
            <Card>
              <CardHeader icon="history" title="Immutable versions" sub="new versions never overwrite prior generation inputs" right={<button className="btn sm" disabled={selectedGuideline.status === 'archived'} onClick={() => setDialog('version')}>Create version</button>} />
              {selectedGuideline.versions.map((version) => <VersionRow key={version.id} guideline={selectedGuideline} version={version} busy={busy} activate={() => { setSelectedId(selectedGuideline.id); setActivationVersion(version.version); setDialog('activate') }} />)}
              {selectedGuideline.status !== 'active' && selectedGuideline.status !== 'archived' && <div className="card-b"><button className="btn danger sm" onClick={() => setDialog('archive')}>Archive unused scope</button></div>}
            </Card>
            <Card><CardHeader icon="history" title="Governance audit" sub="server-attributed" />
              {(load.data.audits[selectedGuideline.id] ?? []).slice().reverse().map((event) => <div className="feed-item" key={event.sequence}><div className="feed-ico human"><Icon name="history" size={14} /></div><div style={{ flex: 1 }}><b>{event.actor}</b> <span className="muted">{titleCase(event.action)} — {event.reason}</span></div><div className="meta">{relTime(event.at)}</div></div>)}
            </Card>
          </>}
        </div>
      </div>
      {dialog === 'create' && brand && <InstructionModal title="Create governed instruction scope" initial={null} busy={busy} close={() => setDialog(null)} submit={(value) => mutate('Instruction scope created.', () => api.create(brand.slug, { ...value, activate: false }))} />}
      {dialog === 'version' && selectedGuideline && <InstructionModal title={`Create ${selectedGuideline.name} version`} initial={selectedGuideline.versions[0]} busy={busy} close={() => setDialog(null)} submit={(value) => mutate('Immutable instruction version created.', () => api.createVersion(selectedGuideline.id, { instructions: value.instructions, rules: value.rules, reason: value.reason, source_ref: value.source_ref }))} versionOnly />}
      {dialog === 'activate' && selectedGuideline && activationVersion !== null && <ReasonModal title={`Activate version ${activationVersion}`} action="Activate exact version" warning="Activation changes future generation inputs and invalidates stale newsletter reviews and approvals in this scope." busy={busy} close={() => setDialog(null)} submit={(reason) => mutate('Instruction version activated. Stale governance evidence was invalidated.', () => api.activate(selectedGuideline.id, activationVersion, reason))} />}
      {dialog === 'archive' && selectedGuideline && <ReasonModal title="Archive instruction scope" action="Archive scope" warning="Only an inactive scope can be archived. Its immutable versions and audit remain available." busy={busy} close={() => setDialog(null)} submit={(reason) => mutate('Unused instruction scope archived.', () => api.archive(selectedGuideline.id, reason))} />}
    </>}
  </Shell>
}

function ActiveInstruction({ guideline }: { guideline: BrandGuideline }) {
  const active = guideline.active_version
  return <Card><CardHeader icon="spark" title="Exact generation input" sub={`${guideline.content_type} · ${guideline.channel}`} right={<Chip kind={active ? 'ok' : 'human'}>{active ? `active v${active.version}` : 'not active'}</Chip>} />
    {!active ? <Empty>No version is active. Generation receives no instruction from this scope.</Empty> : <div className="card-b stack">
      <div className="ai-note"><Icon name="lock" size={15} />This is the exact immutable instruction text and rule object resolved by generation. Editing creates a new inactive version; activation remains a separate human boundary.</div>
      <label className="field"><span>Active instructions</span><textarea className="input mono" readOnly rows={10} value={active.instructions} /></label>
      <label className="field"><span>Active structured rules</span><textarea className="input mono" readOnly rows={9} value={JSON.stringify(active.rules, null, 2)} /></label>
      <div className="meta">Fingerprint <span className="mono">{active.content_fingerprint}</span></div>
      {active.source_ref && <div className="meta">Source <a href={active.source_ref} target="_blank" rel="noreferrer">{active.source_ref}</a></div>}
    </div>}
  </Card>
}

function VersionRow({ guideline, version, busy, activate }: { guideline: BrandGuideline; version: BrandGuidelineVersion; busy: boolean; activate: () => void }) {
  const active = guideline.active_version_id === version.id
  return <div className="card-b campaign-row"><div className="row" style={{ justifyContent: 'space-between' }}><div className="row"><b>Version {version.version}</b>{active && <Chip kind="ok">active</Chip>}</div>{!active && guideline.status !== 'archived' && <button className="btn sm" disabled={busy} onClick={activate}>Activate</button>}</div><div className="meta">{version.change_reason} · {version.created_by} · {relTime(version.created_at)}</div><div className="mono meta">{version.content_fingerprint}</div></div>
}

type InstructionValue = { content_type: string; channel: string; name: string; instructions: string; rules: Record<string, unknown>; reason: string; source_ref: string | null }
function InstructionModal({ title, initial, busy, close, submit, versionOnly = false }: { title: string; initial: BrandGuidelineVersion | null; busy: boolean; close: () => void; submit: (value: InstructionValue) => void; versionOnly?: boolean }) {
  const [contentType, setContentType] = useState('newsletter'), [channel, setChannel] = useState('beehiiv'), [name, setName] = useState('')
  const [instructions, setInstructions] = useState(initial?.instructions ?? ''), [rules, setRules] = useState(JSON.stringify(initial?.rules ?? { operator_checklist: [] }, null, 2))
  const [reason, setReason] = useState(''), [sourceRef, setSourceRef] = useState(initial?.source_ref ?? ''), [jsonError, setJsonError] = useState('')
  const save = () => { try { if ((!versionOnly && (!contentType.trim() || !channel.trim() || !name.trim())) || !instructions.trim() || reason.trim().length < 3) throw new Error('Complete the required fields and provide a specific change reason.'); const parsed = JSON.parse(rules); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Rules must be a JSON object.'); setJsonError(''); submit({ content_type: contentType, channel, name, instructions, rules: parsed, reason, source_ref: sourceRef || null }) } catch (error) { setJsonError(error instanceof Error ? error.message : 'Rules must be valid JSON.') } }
  return <Modal title={title} onClose={close} footer={<><button className="btn" onClick={close}>Cancel</button><button className="btn primary" disabled={busy} onClick={save}>Create inactive version</button></>}>
    <div className="stack">{!versionOnly && <><Field label="Content type" value={contentType} set={setContentType} /><Field label="Channel" value={channel} set={setChannel} /><Field label="Scope name" value={name} set={setName} /></>}<label className="field"><span>Exact generation instructions</span><textarea className="input" required rows={9} value={instructions} onChange={(event) => setInstructions(event.target.value)} /></label><label className="field"><span>Structured rules (JSON object)</span><textarea className="input mono" required rows={9} value={rules} onChange={(event) => setRules(event.target.value)} /></label>{jsonError && <div className="state error">{jsonError}</div>}<Field label="Change reason" value={reason} set={setReason} /><Field label="Source reference (optional)" value={sourceRef} set={setSourceRef} required={false} /><div className="ai-note"><Icon name="lock" size={15} />Saving creates immutable proposed instructions only. It does not activate them or approve content.</div></div>
  </Modal>
}

function ReasonModal({ title, action, warning, busy, close, submit }: { title: string; action: string; warning: string; busy: boolean; close: () => void; submit: (reason: string) => void }) { const [reason, setReason] = useState(''); return <Modal title={title} onClose={close} footer={<><button className="btn" onClick={close}>Cancel</button><button className="btn primary" disabled={busy || reason.trim().length < 3} onClick={() => submit(reason)}>{action}</button></>}><div className="stack"><div className="ai-note"><Icon name="alert" size={15} />{warning}</div><Field label="Operator reason" value={reason} set={setReason} /></div></Modal> }
function Field({ label, value, set, required = true }: { label: string; value: string; set: (value: string) => void; required?: boolean }) { return <label className="field"><span>{label}</span><input className="input" required={required} value={value} onChange={(event) => set(event.target.value)} /></label> }
