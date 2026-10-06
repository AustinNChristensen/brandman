import type { NewsletterRevision } from '../api/types'
import { safeHttpUrl } from '../lib/review'

/** Renders a newsletter revision's material exactly as stored — no rewriting. */
export function NewsletterBody({ revision }: { revision: NewsletterRevision | undefined }) {
  if (!revision) return <span className="meta">No revision content.</span>
  const cta = safeHttpUrl(revision.cta?.url)
  return (
    <div className="stack" style={{ gap: 12, fontSize: 14.5, lineHeight: 1.6 }}>
      <div>
        <div style={{ fontSize: 22, fontWeight: 600, letterSpacing: '-0.01em', lineHeight: 1.25 }}>{revision.final_title || revision.working_title || 'Untitled'}</div>
        <div className="meta" style={{ marginTop: 4 }}>Subject: {revision.subject || '—'} · Preview: {revision.preview_text || '—'}</div>
      </div>
      {revision.editorial_thesis && <div className="muted" style={{ fontSize: 13 }}><b>Thesis</b> — {revision.editorial_thesis}</div>}
      {(revision.sections ?? []).map((s, i) => (
        <div key={i}>
          {s.heading && <div style={{ fontWeight: 600, marginBottom: 2 }}>{s.heading}</div>}
          <div style={{ whiteSpace: 'pre-wrap' }}>{s.body}</div>
        </div>
      ))}
      {revision.cta?.label && (
        <div>
          {cta ? <a href={cta} target="_blank" rel="noopener noreferrer" className="btn">{revision.cta.label}</a>
            : <span className="btn" style={{ opacity: .6 }} title="non-HTTPS link blocked">{revision.cta.label} · non-HTTPS link blocked</span>}
        </div>
      )}
      {(revision.claims?.length ?? 0) > 0 && (
        <div className="stack" style={{ gap: 6, fontSize: 12.5, borderTop: '1px solid var(--line-2)', paddingTop: 10 }}>
          <div style={{ fontWeight: 600 }}>Claims and citations</div>
          {revision.claims.map((c, i) => (
            <div key={String(c.id ?? c.claim_id ?? i)}>
              <span className="mono" style={{ color: 'var(--faint)' }}>{String(c.id ?? c.claim_id ?? i)}</span> {String(c.text ?? c.statement ?? '')}
              {(c.citations ?? []).map((cit, j) => {
                const url = safeHttpUrl(cit.url)
                return <div key={j} className="meta" style={{ paddingLeft: 14 }}>↳ source {cit.source_id ?? '?'}{url ? <> · <a href={url} target="_blank" rel="noopener noreferrer">{url}</a></> : cit.url ? ' · non-HTTPS link blocked' : ''}</div>
              })}
            </div>
          ))}
        </div>
      )}
      {(revision.source_provenance?.length ?? 0) > 0 && (
        <div className="stack" style={{ gap: 4, fontSize: 12.5 }}>
          <div style={{ fontWeight: 600 }}>Source provenance</div>
          {revision.source_provenance.map((s, i) => {
            const url = safeHttpUrl(s.url)
            return <div key={i} className="muted"><span className="mono">{String(s.source_id ?? '?')}</span> {String(s.title ?? '')}{url ? <> · <a href={url} target="_blank" rel="noopener noreferrer">{url}</a></> : ''}</div>
          })}
        </div>
      )}
      {revision.content_basis?.statement && <div className="meta">Basis: {revision.content_basis.kind ?? ''} — {revision.content_basis.statement}</div>}
    </div>
  )
}
