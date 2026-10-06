import { useState } from 'react'
import { brands as brandsApi, learningLab } from '../api/endpoints'
import type { Campaign, ContentExperiment, Learning } from '../api/types'
import { describe, useLoad } from '../api/useLoad'
import { Icon } from '../components/icons'
import { Shell } from '../components/Shell'
import { Card, CardHeader, Chip, Empty, ErrorState, Loading, Modal, StatusChip } from '../components/ui'
import { shortDateTime, titleCase } from '../lib/format'
import { useBrands } from '../state/BrandContext'
import { useToast } from '../state/Toast'

type Dialog = 'learning' | 'experiment' | null

async function loadLab(slug: string) {
  const [learnings, experiments, context] = await Promise.all([
    learningLab.list(slug), learningLab.experiments(slug), brandsApi.context(slug),
  ])
  return { learnings, experiments, campaigns: context.campaigns }
}

export default function Learnings() {
  const { selected, active } = useBrands()
  const brand = selected ? active[0] : undefined
  const load = useLoad(() => brand ? loadLab(brand.slug) : Promise.resolve(null), [brand?.slug])
  const [learningId, setLearningId] = useState<string | null>(null)
  const [experimentId, setExperimentId] = useState<string | null>(null)
  const [dialog, setDialog] = useState<Dialog>(null)
  const { notify } = useToast()
  const effectiveLearningId = learningId && load.data?.learnings.some((item) => item.id === learningId)
    ? learningId : load.data?.learnings[0]?.id ?? null
  const effectiveExperimentId = experimentId && load.data?.experiments.some((item) => item.id === experimentId)
    ? experimentId : load.data?.experiments[0]?.id ?? null
  const audit = useLoad(
    () => brand && effectiveLearningId ? learningLab.audit(brand.slug, effectiveLearningId) : Promise.resolve([]),
    [brand?.slug, effectiveLearningId],
  )
  const detail = useLoad(
    () => brand && effectiveExperimentId ? learningLab.experiment(brand.slug, effectiveExperimentId) : Promise.resolve(null),
    [brand?.slug, effectiveExperimentId],
  )
  const learning = load.data?.learnings.find((item) => item.id === effectiveLearningId)
  const experiment = detail.data ?? load.data?.experiments.find((item) => item.id === effectiveExperimentId)
  const refresh = () => { load.reload(); audit.reload(); detail.reload() }
  const act = async (message: string, action: () => Promise<unknown>) => {
    try { await action(); notify(message); refresh() }
    catch (error) { notify(describe(error), 'bad') }
  }

  if (!selected) return <Shell title="Learnings & experiments"><Card><Empty>Select one brand to review evidence and make human decisions.</Empty></Card></Shell>
  return <Shell title="Learnings & experiments" crumb={brand?.name} right={<Chip kind="human" icon="lock">human acceptance required</Chip>}>
    {load.error && <ErrorState message={load.error} retry={load.reload} />}
    {load.loading && !load.data && <Loading label="Loading evidence lab…" />}
    {load.data && <div className="stack" style={{ gap: 16 }}>
      <div className="ai-note"><Icon name="bulb" size={16} /><span>Recommendations remain inert until a human accepts them. Accepted learnings are bounded planning priors—not automatic instructions, approvals, or publishing authority.</span></div>
      <div className="row" style={{ justifyContent: 'flex-end', flexWrap: 'wrap' }}>
        <button className="btn" onClick={() => setDialog('experiment')}><Icon name="chart" size={14} />New controlled test</button>
        <button className="btn primary" onClick={() => setDialog('learning')}><Icon name="plus" size={14} />Propose learning</button>
      </div>
      <section className="learning-layout">
        <Card className="learning-list"><CardHeader title="Learning lifecycle" sub={`${load.data.learnings.length}`} />
          {!load.data.learnings.length ? <Empty>No proposals yet.</Empty> : load.data.learnings.map((item) =>
            <button className={`list-item ${learning?.id === item.id ? 'on' : ''}`} key={item.id} onClick={() => setLearningId(item.id)}>
              <div className="row" style={{ justifyContent: 'space-between' }}><b>{item.hypothesis}</b><StatusChip status={item.status} /></div>
              <div className="meta">{item.proposed_change}</div>
            </button>)}
        </Card>
        <Card>{learning ? <>
          <CardHeader icon="bulb" title={learning.hypothesis} sub={`proposed ${shortDateTime(learning.created_at)}`} right={<StatusChip status={learning.status} />} />
          <div className="card-b stack">
            <div><div className="meta">Proposed change</div><div>{learning.proposed_change}</div></div>
            <div className="grid learning-two-col">
              <div><div className="meta">Supporting evidence</div><div>{learning.evidence}</div></div>
              <div><div className="meta">Scope</div><code>{JSON.stringify(learning.scope)}</code></div>
            </div>
            <div className="row" style={{ flexWrap: 'wrap' }}>
              {learning.status === 'proposed' && <button className="btn" onClick={() => void act('Learning entered testing.', () => learningLab.transition(brand!.slug, learning.id, 'testing'))}>Start test</button>}
              {learning.status === 'proposed' && <button className="btn danger" onClick={() => window.confirm('Reject this proposal? The audit trail remains available.') && void act('Learning rejected.', () => learningLab.transition(brand!.slug, learning.id, 'reject'))}>Reject</button>}
              {learning.status === 'testing' && <button className="btn primary" onClick={() => window.confirm('Accept this as an active bounded planning prior?') && void act('Learning accepted by the signed-in reviewer.', () => learningLab.transition(brand!.slug, learning.id, 'accept'))}>Accept evidence</button>}
              {learning.status === 'testing' && <button className="btn danger" onClick={() => window.confirm('Reject this tested hypothesis?') && void act('Learning rejected.', () => learningLab.transition(brand!.slug, learning.id, 'reject'))}>Reject</button>}
              {learning.status === 'accepted' && learning.active && <button className="btn" onClick={() => void act('Learning disabled.', () => learningLab.transition(brand!.slug, learning.id, 'disable', 'Operator paused this prior for review.'))}>Disable prior</button>}
              {learning.status === 'accepted' && !learning.active && <button className="btn" onClick={() => void act('Learning enabled.', () => learningLab.transition(brand!.slug, learning.id, 'enable', 'Operator restored this reviewed prior.'))}>Enable prior</button>}
              {learning.status === 'accepted' && <button className="btn danger" onClick={() => window.confirm('Supersede this learning? It will stop influencing plans.') && void act('Learning superseded.', () => learningLab.transition(brand!.slug, learning.id, 'supersede'))}>Supersede</button>}
            </div>
          </div>
          <div className="card-b"><div className="meta" style={{ marginBottom: 8 }}>Immutable review history</div>{audit.error && <ErrorState message={audit.error} retry={audit.reload} />}{audit.data?.map((event) => <div className="feed-item" key={event.sequence}><div><b>{titleCase(event.action)}</b><div className="meta">{event.actor} · {shortDateTime(event.at)}</div></div></div>)}</div>
        </> : <Empty>Select or propose a learning.</Empty>}</Card>
      </section>
      <section className="learning-layout">
        <Card className="learning-list"><CardHeader title="Controlled experiments" sub={`${load.data.experiments.length}`} />
          {!load.data.experiments.length ? <Empty>No controlled tests yet.</Empty> : load.data.experiments.map((item) =>
            <button className={`list-item ${experiment?.id === item.id ? 'on' : ''}`} key={item.id} onClick={() => setExperimentId(item.id)}>
              <div className="row" style={{ justifyContent: 'space-between' }}><b>{item.hypothesis}</b><StatusChip status={item.status} /></div>
              <div className="meta">{item.metric} · {item.measurement_windows.length} measurement window(s)</div>
            </button>)}
        </Card>
        <ExperimentDetail experiment={experiment} slug={brand!.slug} act={act} />
      </section>
    </div>}
    {dialog === 'learning' && <LearningModal slug={brand!.slug} close={() => setDialog(null)} done={(item) => { setLearningId(item.id); setDialog(null); refresh() }} />}
    {dialog === 'experiment' && <ExperimentModal slug={brand!.slug} campaigns={load.data?.campaigns ?? []} close={() => setDialog(null)} done={(item) => { setExperimentId(item.id); setDialog(null); refresh() }} />}
  </Shell>
}

function ExperimentDetail({ experiment, slug, act }: { experiment?: ContentExperiment | null; slug: string; act: (message: string, action: () => Promise<unknown>) => Promise<void> }) {
  if (!experiment) return <Card><Empty>Select or create a controlled test.</Empty></Card>
  const current = experiment.recommendations[0]
  return <Card><CardHeader icon="chart" title={experiment.hypothesis} sub={`${experiment.metric} · source-grounded X drafts`} right={<StatusChip status={experiment.status} />} />
    <div className="card-b grid learning-two-col">{experiment.variants.map((variant) => <div className="learning-variant" key={variant.id}><div className="row"><b>{titleCase(variant.variant_key)}</b><StatusChip status={variant.post_status} /></div><div className="xpost">{variant.body}</div><div className="meta">{variant.rationale}</div></div>)}</div>
    <div className="card-b"><div className="meta" style={{ marginBottom: 8 }}>Declared measurement windows</div>{experiment.measurement_windows.map((window) => <div className="learning-window" key={window.id}><div className="row"><b>{window.window_key}</b><StatusChip status={window.status} /><Chip kind="neutral">{window.evidence_state}</Chip></div><div className="meta">observe {shortDateTime(window.opens_at)} – {shortDateTime(window.closes_at)} · evaluate {shortDateTime(window.evaluate_at)}</div></div>)}</div>
    <div className="card-b stack"><div className="row" style={{ justifyContent: 'space-between' }}><div><b>Evidence recommendation</b><div className="meta">A recommendation does not alter content or approve publishing.</div></div>{current && <StatusChip status={current.status} />}</div>
      {current && <><div>{current.rationale}</div><div className="grid learning-two-col">{current.evidence.map((evidence) => <div key={evidence.variant_id}><div className="meta">{titleCase(evidence.variant_key)}</div><b>{evidence.metric_value} {current.metric}</b><div className="meta">{evidence.impressions} impressions · {evidence.observation_count} observations</div></div>)}</div></>}
      <div className="row" style={{ flexWrap: 'wrap' }}>
        {experiment.status === 'active' && <button className="btn" onClick={() => void act('Evidence evaluated.', () => learningLab.recommend(slug, experiment.id))}>Evaluate due window</button>}
        {experiment.status === 'active' && current?.status === 'recommended' && <button className="btn primary" onClick={() => window.confirm('Accept this exact recommendation as a proposed learning? It will still require testing before use.') && void act('Recommendation accepted by the signed-in reviewer.', () => learningLab.acceptRecommendation(slug, experiment.id, current.id))}>Accept recommendation</button>}
      </div>
      {current?.accepted_by && <div className="ai-note"><Icon name="lock" size={14} />Accepted by {current.accepted_by} at {shortDateTime(current.accepted_at)}. The resulting learning remains proposed and inert until separately tested and accepted.</div>}
    </div>
  </Card>
}

function LearningModal({ slug, close, done }: { slug: string; close: () => void; done: (item: Learning) => void }) {
  const [hypothesis, setHypothesis] = useState(''), [change, setChange] = useState(''), [evidence, setEvidence] = useState(''), [channel, setChannel] = useState('x'), [busy, setBusy] = useState(false), { notify } = useToast()
  const submit = async () => { setBusy(true); try { done(await learningLab.propose(slug, { hypothesis, evidence, proposed_change: change, scope: { channel }, uncertainty: { status: 'requires_human_testing' } })); notify('Learning proposed. It is inert until tested and accepted.') } catch (error) { notify(describe(error), 'bad') } finally { setBusy(false) } }
  return <Modal title="Propose an evidence-backed learning" onClose={close} footer={<><button className="btn" onClick={close}>Cancel</button><button className="btn primary" disabled={busy || !hypothesis || !change || !evidence} onClick={() => void submit()}>Propose only</button></>}><Field label="Hypothesis" value={hypothesis} set={setHypothesis} /><Field label="Proposed bounded change" value={change} set={setChange} /><Field label="Supporting evidence" value={evidence} set={setEvidence} area /><Select label="Scope" value={channel} set={setChannel} options={['x', 'newsletter', 'web']} /><div className="meta">This creates no content, approval, schedule, or provider action.</div></Modal>
}

function ExperimentModal({ slug, campaigns, close, done }: { slug: string; campaigns: Campaign[]; close: () => void; done: (item: ContentExperiment) => void }) {
  const grounded = campaigns.filter((item) => item.source_id)
  const [campaign, setCampaign] = useState(grounded[0]?.id ?? ''), [hypothesis, setHypothesis] = useState(''), [metric, setMetric] = useState<ContentExperiment['metric']>('clicks'), [hours, setHours] = useState('24'), [busy, setBusy] = useState(false), { notify } = useToast()
  const submit = async () => { const opens = new Date(), closes = new Date(opens.getTime() + Number(hours) * 3600000), late = new Date(closes.getTime() + 48 * 3600000); setBusy(true); try { done(await learningLab.draftExperiment(slug, { campaign_id: campaign, hypothesis, metric, guardrails: { min_observations_per_variant: 1, min_impressions_per_variant: 10 }, measurement_windows: [{ window_key: 'primary', opens_at: opens.toISOString(), closes_at: closes.toISOString(), evaluate_at: closes.toISOString(), late_evidence_until: late.toISOString() }] })); notify('Controlled test created with draft variants only.') } catch (error) { notify(describe(error), 'bad') } finally { setBusy(false) } }
  return <Modal title="Create a source-grounded controlled test" onClose={close} footer={<><button className="btn" onClick={close}>Cancel</button><button className="btn primary" disabled={busy || !campaign || !hypothesis} onClick={() => void submit()}>Create draft test</button></>}><Select label="Source-grounded campaign" value={campaign} set={setCampaign} options={grounded.map((item) => [item.id, item.name])} empty="Select a campaign" /><Field label="Hypothesis" value={hypothesis} set={setHypothesis} /><Select label="Primary metric" value={metric} set={(value) => setMetric(value as ContentExperiment['metric'])} options={['clicks', 'impressions', 'engagements', 'conversions']} /><Field label="Measurement window (hours)" value={hours} set={setHours} type="number" /><div className="meta">Both variants stay draft. Collection is aggregate-only; accepting a winner never publishes either variant.</div></Modal>
}

function Field({ label, value, set, type = 'text', area = false }: { label: string; value: string; set: (value: string) => void; type?: string; area?: boolean }) { return <label className="field"><span>{label}</span>{area ? <textarea aria-label={label} className="input" required value={value} onChange={(event) => set(event.target.value)} rows={3} /> : <input aria-label={label} className="input" type={type} required value={value} min={type === 'number' ? 1 : undefined} onChange={(event) => set(event.target.value)} />}</label> }
function Select({ label, value, set, options, empty }: { label: string; value: string; set: (value: string) => void; options: string[] | string[][]; empty?: string }) { return <label className="field"><span>{label}</span><select aria-label={label} className="input" value={value} onChange={(event) => set(event.target.value)}>{empty && <option value="">{empty}</option>}{options.map((option) => { const pair = typeof option === 'string' ? [option, titleCase(option)] : option; return <option key={pair[0]} value={pair[0]}>{pair[1]}</option> })}</select></label> }
