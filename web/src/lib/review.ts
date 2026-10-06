import type { ApprovalScope, FactCheck, NewsletterIssue } from '../api/types'

/**
 * Client-side mirror of the server's newsletter review gate. The server is
 * authoritative (it re-checks on approve); this only decides whether to
 * enable the approve control so operators aren't offered a dead end.
 */
export function newsletterReviewReady(issue: NewsletterIssue, evidence: FactCheck | null): { ready: boolean; reasons: string[] } {
  const reasons: string[] = []
  const scope = issue.approval_scope
  if (!scope) reasons.push('approval scope missing — reload the queue')
  if (issue.lifecycle !== 'fact_checked') reasons.push(`lifecycle is ${issue.lifecycle}, needs fact_checked`)
  if (!evidence) reasons.push('no fact-check recorded for the current revision')
  if (scope && scope.resource_id !== issue.id) reasons.push('scope points at a different issue')
  if (scope && scope.revision !== issue.current_revision) reasons.push('scope revision is stale')
  if (scope && scope.action_type !== 'create_draft') reasons.push('scope action is not create_draft')
  if (scope && scope.destination !== 'beehiiv:draft') reasons.push('scope destination is not beehiiv:draft')
  if (scope && !String(scope.review_token || '').startsWith('review-v1:')) reasons.push('review token malformed')
  if (evidence) {
    if (evidence.revision !== issue.current_revision) reasons.push('fact-check is for an older revision')
    if (!evidence.passed) reasons.push('fact-check did not pass')
    if (!evidence.reviewer) reasons.push('fact-check has no reviewer')
    if (scope && evidence.content_fingerprint !== scope.material_fingerprint) reasons.push('fact-check fingerprint does not match the approval scope')
    const claims = issue.content?.claims ?? []
    const claimIds = [...new Set(claims.map((c) => String(c.id ?? c.claim_id ?? '')).filter(Boolean))]
    const verdictIds = [...new Set(evidence.verdicts.map((v) => String(v.claim_id)))]
    if (claimIds.length !== verdictIds.length || claimIds.some((id) => !verdictIds.includes(id))) reasons.push('claim ids and verdicts do not match 1:1')
    if (!evidence.verdicts.every((v) => v.verified === true)) reasons.push('not every claim verdict is verified')
    const sourceIds = new Set((issue.content?.source_provenance ?? []).map((s) => String(s.source_id ?? '')))
    for (const claim of claims) {
      for (const citation of claim.citations ?? []) {
        if (!sourceIds.has(String(citation.source_id ?? ''))) { reasons.push('a citation does not resolve to source provenance'); break }
      }
    }
  }
  return { ready: reasons.length === 0, reasons }
}

export function scopeSummary(scope: ApprovalScope): { k: string; v: string }[] {
  return [
    { k: 'Action', v: scope.action_type },
    { k: 'Destination', v: scope.destination },
    { k: 'Account', v: scope.account_ref },
    { k: 'Revision', v: String(scope.revision) },
    { k: 'Schedule', v: scope.intended_schedule ?? 'none' },
    { k: 'Campaign', v: scope.campaign_id ?? '—' },
    { k: 'Fingerprint', v: scope.material_fingerprint },
  ]
}

export function safeHttpUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try {
    const parsed = new URL(value)
    return parsed.protocol === 'https:' ? parsed.toString() : null
  } catch { return null }
}
