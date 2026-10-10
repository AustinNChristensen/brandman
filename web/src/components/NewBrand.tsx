import { useState, type FormEvent } from 'react'
import { brands, type NewBrandInput } from '../api/endpoints'
import { describe } from '../api/useLoad'
import { Card, CardHeader } from './ui'

export function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60)
}

/** Create a brand workspace. Agents and people then work from its context. */
export function NewBrandCard({ onCreated }: { onCreated: (slug: string) => void }) {
  const [name, setName] = useState(''), [slug, setSlug] = useState(''), [slugEdited, setSlugEdited] = useState(false)
  const [mission, setMission] = useState(''), [voice, setVoice] = useState('Clear, specific, no generic marketing fluff.')
  const [rules, setRules] = useState('Verify claims, dates and sources before publishing.')
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null)
  const effectiveSlug = slugEdited ? slug : slugify(name)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(null)
    const body: NewBrandInput = { slug: effectiveSlug, name, mission, voice, compliance_rules: rules, approval_policy: 'human_approval_required' }
    try { await brands.create(body); onCreated(effectiveSlug) } catch (failure) { setError(describe(failure)) } finally { setBusy(false) }
  }
  return <Card><CardHeader icon="plus" title="Create a brand" sub="mission, voice and rules every agent and person works from" />
    <form className="stack card-b" onSubmit={submit}>
      <label className="field"><span>Brand name</span><input className="input" required value={name} onChange={(event) => setName(event.target.value)} /></label>
      <label className="field"><span>Slug</span><input className="input" required pattern="[a-z0-9-]+" value={effectiveSlug} onChange={(event) => { setSlugEdited(true); setSlug(event.target.value) }} /></label>
      <label className="field"><span>Mission</span><textarea className="input" required value={mission} onChange={(event) => setMission(event.target.value)} /></label>
      <label className="field"><span>Voice</span><textarea className="input" required value={voice} onChange={(event) => setVoice(event.target.value)} /></label>
      <label className="field"><span>Compliance rules</span><textarea className="input" required value={rules} onChange={(event) => setRules(event.target.value)} /></label>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="row" style={{ justifyContent: 'flex-end' }}><button className="btn primary" disabled={busy || !effectiveSlug}>Create brand</button></div>
    </form></Card>
}
