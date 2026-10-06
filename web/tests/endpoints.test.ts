import { afterEach, describe, expect, it, vi } from 'vitest'
import { campaignGraphs, campaignPosts, dispatch, editorialCandidates, execution, integrations, newsletters, performance, productFeedback, publishingPlan, settings, sourceInputs } from '../src/api/endpoints'

function response(body = '{}') {
  return { ok: true, status: 200, statusText: 'OK', text: async () => body } as Response
}

afterEach(() => vi.unstubAllGlobals())

describe('governed mutation contracts', () => {
  it('binds approvals to the exact revision and review token without an actor', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)

    await dispatch.approve('post/123', 4, 'dispatch-token')
    await newsletters.approve('issue/456', 7, 'newsletter-token')

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/dispatch-items/post%2F123/approve',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ revision: 4, review_token: 'dispatch-token' }) }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/newsletter-issues/issue%2F456/approve',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ revision: 7, review_token: 'newsletter-token' }) }))
    expect(fetchMock.mock.calls.map((call) => String(call[1].body))).not.toEqual(
      expect.arrayContaining([expect.stringContaining('actor')]),
    )
  })

  it('sends fact-check verdicts and rejection reasons to the governed endpoints', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)
    const verdicts = [{ claim_id: 'claim-1', verified: true, notes: 'Canonical source checked' }]

    await newsletters.recordFactCheck('issue-1', 3, verdicts, 'Review complete')
    await newsletters.reject('issue-1', 3, 'Needs a clearer transfer warning')

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/newsletter-issues/issue-1/fact-check',
      expect.objectContaining({ body: JSON.stringify({ revision: 3, verdicts, notes: 'Review complete' }) }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/newsletter-issues/issue-1/reject',
      expect.objectContaining({ body: JSON.stringify({ revision: 3, reason: 'Needs a clearer transfer warning' }) }))
  })

  it('keeps X confirmation exact and separate from provider execution', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)
    const fingerprint = `sha256:${'a'.repeat(64)}`

    await execution.confirmX('task/1', 8, fingerprint)

    expect(fetchMock).toHaveBeenCalledWith('/api/execution-tasks/task%2F1/confirm-public-action',
      expect.objectContaining({ body: JSON.stringify({
        expected_revision: 8,
        expected_material_fingerprint: fingerprint,
        confirmation_phrase: 'CONFIRM PUBLIC X POST',
        validity_seconds: 300,
      }) }))
    expect(String(fetchMock.mock.calls[0][0])).not.toMatch(/publish|schedule|send/)
  })

  it('prepares and connects an exact least-privilege lane through brand-owned endpoints', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)
    const lane = { lane: 'beehiiv_read', provider: 'beehiiv' as const, title: 'Beehiiv insights', purpose: 'Read metrics', scopes: ['posts.read'], capabilities: ['posts.read', 'metrics.read'], credential_inputs: [{ key: 'api_key', label: 'API token', secret: true }], can_read: true, can_write: false, write_boundary: 'Read only.' }
    await integrations.prepareLane('demo brand', lane, 'pub/id', 'Insights')
    await integrations.connect('demo brand', lane, 'pub/id', 'Insights', { api_key: 'write-only' })
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/brands/points%20mafia/connectors', expect.objectContaining({ body: JSON.stringify({ connector_type: 'beehiiv', account_key: 'pub/id', display_name: 'Insights', status: 'disconnected', scopes: ['posts.read'], capabilities: ['posts.read', 'metrics.read'], configuration: { connection_role: 'beehiiv_read', delivery_mode: 'api' } }) }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/brands/points%20mafia/connections/beehiiv/pub%2Fid', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ display_name: 'Insights', credentials: { api_key: 'write-only' }, required_scopes: ['posts.read'], granted_scopes: ['posts.read'] }) }))
  })

  it('exposes every newsletter completion boundary with exact encoded identity and payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response('[]'))
    vi.stubGlobal('fetch', fetchMock)
    const id = 'issue/r2'

    await newsletters.policyReview(id, 2, { one_thesis: true })
    await newsletters.authorizeQuickHit(id, 2, 'Time-sensitive offer expires today.')
    await newsletters.revise(id, { preview_text: 'Revised' }, 'Tighten preview')
    await newsletters.abandon(id, 'Superseded by a newer issue')
    await newsletters.archive(id, 'Cleanup reviewed')
    await newsletters.createDistributionPackage(id, { expected_revision: 2, idempotency_key: 'r2' })
    await newsletters.exportPreview(id)
    await newsletters.exportJobs(id)
    await newsletters.providerReconciliations(id)

    const calls = fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])
    expect(calls).toEqual([
      ['/api/newsletter-issues/issue%2Fr2/policy-review', 'POST', JSON.stringify({ revision: 2, checklist: { one_thesis: true } })],
      ['/api/newsletter-issues/issue%2Fr2/quick-hit-authorization', 'POST', JSON.stringify({ revision: 2, reason: 'Time-sensitive offer expires today.' })],
      ['/api/newsletter-issues/issue%2Fr2', 'PATCH', JSON.stringify({ changes: { preview_text: 'Revised' }, change_note: 'Tighten preview' })],
      ['/api/newsletter-issues/issue%2Fr2/abandon', 'POST', JSON.stringify({ reason: 'Superseded by a newer issue' })],
      ['/api/newsletter-issues/issue%2Fr2/archive', 'POST', JSON.stringify({ reason: 'Cleanup reviewed' })],
      ['/api/newsletter-issues/issue%2Fr2/distribution-package', 'POST', JSON.stringify({ expected_revision: 2, idempotency_key: 'r2' })],
      ['/api/newsletter-issues/issue%2Fr2/export-preview', 'GET', undefined],
      ['/api/newsletter-issues/issue%2Fr2/export-jobs', 'GET', undefined],
      ['/api/newsletter-issues/issue%2Fr2/provider-reconciliations', 'GET', undefined],
    ])
  })

  it('keeps campaign graph mutations server-attributed and free of publish commands', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)
    await campaignGraphs.anchor('membership/1', 'Make the newsletter primary')
    await campaignGraphs.reorder('campaign/1', ['m2', 'm1'], 'Reflect the reader path')
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/campaign-memberships/membership%2F1/anchor', expect.objectContaining({ body: JSON.stringify({ reason: 'Make the newsletter primary' }) }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/campaigns/campaign%2F1/memberships/reorder', expect.objectContaining({ body: JSON.stringify({ membership_ids: ['m2', 'm1'], reason: 'Reflect the reader path' }) }))
    const bodies = fetchMock.mock.calls.map((call) => String(call[1].body))
    expect(bodies.join(' ')).not.toMatch(/actor|publish|schedule|send/)
  })

  it('keeps publishing-plan reflow separate from provider scheduling and supports exact undo', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)
    const item = {
      item_type: 'post' as const, item_id: 'post/1', channel: 'x', status: 'draft',
      title: 'Draft', initiative_id: 'initiative-1', planned_for: null,
      pinned: true, locked: false,
    }
    await publishingPlan.updateSettings('demo brand', {
      timezone: 'America/Denver', windows: [{ weekday: 0, start: '09:00', end: '17:00' }],
      cadence_minutes: { x: 120, newsletter: 1440 },
    })
    await publishingPlan.updateItem('demo brand', item)
    await publishingPlan.preview('demo brand', '2026-09-07T15:00:00.000Z')
    await publishingPlan.commit('demo brand', 'preview/1')
    await publishingPlan.undo('demo brand', 'commit/1')

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/api/brands/points%20mafia/publishing-plan/settings', 'PUT', JSON.stringify({ timezone: 'America/Denver', windows: [{ weekday: 0, start: '09:00', end: '17:00' }], cadence_minutes: { x: 120, newsletter: 1440 } })],
      ['/api/brands/points%20mafia/publishing-plan/items/post/post%2F1', 'PUT', JSON.stringify({ initiative_id: 'initiative-1', planned_for: null, pinned: true, locked: false })],
      ['/api/brands/points%20mafia/publishing-plan/reflow/preview', 'POST', JSON.stringify({ start_at: '2026-09-07T15:00:00.000Z' })],
      ['/api/brands/points%20mafia/publishing-plan/reflow/commit', 'POST', JSON.stringify({ preview_id: 'preview/1' })],
      ['/api/brands/points%20mafia/publishing-plan/reflow/undo', 'POST', JSON.stringify({ commit_id: 'commit/1' })],
    ])
    expect(fetchMock.mock.calls.map(([url]) => String(url)).join(' ')).not.toMatch(/publish$|\/schedule/)
  })

  it('keeps source onboarding configuration-only and performance planning read-only', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response('[]'))
    vi.stubGlobal('fetch', fetchMock)

    await sourceInputs.onboard('demo brand', {
      publisher_name: 'Public publisher', feed_url: 'https://publisher.test/rss',
      feed_format: 'rss', polling_interval_seconds: 1800, reason: 'Relevant public reporting',
    })
    await sourceInputs.revalidate('demo brand', 'source/1', 'operator:source/1:2026-09-03')
    await performance.planning('demo brand', 'newsletter', 'award travel')

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/api/brands/points%20mafia/third-party-sources', 'POST', JSON.stringify({ publisher_name: 'Public publisher', feed_url: 'https://publisher.test/rss', feed_format: 'rss', polling_interval_seconds: 1800, reason: 'Relevant public reporting' })],
      ['/api/brands/points%20mafia/sources/source%2F1/canonical-revalidations', 'POST', JSON.stringify({ idempotency_key: 'operator:source/1:2026-09-03' })],
      ['/api/brands/points%20mafia/performance-planning?stage=portfolio&channel=newsletter&topic=award%20travel', 'GET', undefined],
    ])
    expect(fetchMock.mock.calls.map(([, init]) => String(init?.body ?? '')).join(' ')).not.toMatch(/"credentials"|"subscriber|"publish"|"send"/)
  })

  it('creates, revises, and resubmits content without caller-supplied authorship', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)
    await dispatch.create('demo brand', 'x', { body: 'Draft' })
    await dispatch.edit('draft/1', { body: 'Revision' })
    await dispatch.submit('draft/1')
    await newsletters.create('demo brand', { working_title: 'Issue' }, 'candidate/1')
    await editorialCandidates.create('demo brand', { title: 'Idea', summary: 'Useful', dimensions: { relevance: 1 }, recommended_treatment: 'newsletter' })
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/api/brands/points%20mafia/dispatch-items', 'POST', JSON.stringify({ connector: 'x', payload: { body: 'Draft' } })],
      ['/api/dispatch-items/draft%2F1', 'PATCH', JSON.stringify({ payload: { body: 'Revision' } })],
      ['/api/dispatch-items/draft%2F1/submit', 'POST', JSON.stringify({})],
      ['/api/brands/points%20mafia/newsletter-issues', 'POST', JSON.stringify({ content: { working_title: 'Issue' }, candidate_id: 'candidate/1' })],
      ['/api/brands/points%20mafia/editorial-candidates', 'POST', JSON.stringify({ title: 'Idea', summary: 'Useful', dimensions: { relevance: 1 }, recommended_treatment: 'newsletter' })],
    ])
    expect(JSON.stringify(fetchMock.mock.calls)).not.toMatch(/actor|created_by|publish|schedule|send/)
  })

  it('uses only brand-scoped settings, schedule, pricing, and bounded tick contracts', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)
    await settings.update('demo brand', { mission: 'Mission', voice: 'Voice', compliance_rules: 'Rules', approval_policy: 'human_approval_required', reason: 'Reviewed change' })
    await settings.addRateCard('demo brand', { version: 'customer-1', operation: 'beehiiv_read', unit_price: '0.002', currency: 'USD', effective_at: '2026-09-03T00:00:00Z' })
    await settings.setSchedule('demo brand', 'connector/bee/posts', false)
    await settings.tick('demo brand', 25)
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/api/brands/points%20mafia/settings', 'PATCH', JSON.stringify({ mission: 'Mission', voice: 'Voice', compliance_rules: 'Rules', approval_policy: 'human_approval_required', reason: 'Reviewed change' })],
      ['/api/brands/points%20mafia/provider-rate-cards', 'POST', JSON.stringify({ version: 'customer-1', operation: 'beehiiv_read', unit_price: '0.002', currency: 'USD', effective_at: '2026-09-03T00:00:00Z' })],
      ['/api/brands/points%20mafia/orchestration/schedules/connector%2Fbee%2Fposts', 'PUT', JSON.stringify({ enabled: false })],
      ['/api/brands/points%20mafia/orchestration/tick', 'POST', JSON.stringify({ max_decisions: 25 })],
    ])
    expect(fetchMock.mock.calls.map(([url]) => String(url)).join(' ')).not.toMatch(/^\/api\/orchestration\/tick|publish|send/)
  })

  it('uses brand-scoped feedback lifecycle routes without caller-supplied actors', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response('[]'))
    vi.stubGlobal('fetch', fetchMock)
    await productFeedback.get('demo brand', 'failure/1')
    await productFeedback.history('demo brand', 'failure/1')
    await productFeedback.comment('demo brand', 'failure/1', 'Reproduced by operator')
    await productFeedback.start('demo brand', 'failure/1', { assignee: 'builder' })
    await productFeedback.resolve('demo brand', 'failure/1', { resolution_evidence: 'Regression passes' })
    await productFeedback.verify('demo brand', 'failure/1', 'Verified in browser')
    await productFeedback.reopen('demo brand', 'failure/1', 'Failure recurred')
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/api/brands/points%20mafia/product-feedback/failure%2F1', 'GET', undefined],
      ['/api/brands/points%20mafia/product-feedback/failure%2F1/history', 'GET', undefined],
      ['/api/brands/points%20mafia/product-feedback/failure%2F1/comments', 'POST', JSON.stringify({ body: 'Reproduced by operator' })],
      ['/api/brands/points%20mafia/product-feedback/failure%2F1/start', 'POST', JSON.stringify({ assignee: 'builder' })],
      ['/api/brands/points%20mafia/product-feedback/failure%2F1/resolve', 'POST', JSON.stringify({ resolution_evidence: 'Regression passes' })],
      ['/api/brands/points%20mafia/product-feedback/failure%2F1/verify', 'POST', JSON.stringify({ evidence: 'Verified in browser' })],
      ['/api/brands/points%20mafia/product-feedback/failure%2F1/reopen', 'POST', JSON.stringify({ reason: 'Failure recurred' })],
    ])
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain('actor')
  })

  it('authors canonical campaign posts and candidate promotions as drafts without actors or provider actions', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response())
    vi.stubGlobal('fetch', fetchMock)
    await campaignPosts.create('campaign/1', 'x', 'Canonical draft')
    await campaignPosts.edit('post/1', 'Canonical revision')
    await campaignPosts.audit('post/1')
    await campaignPosts.createDispatch('post/1')
    await editorialCandidates.promoteToCampaignPost('demo brand', 'candidate/1', {
      campaign_id: null, campaign_name: 'Draft campaign', objective: 'Help readers decide',
      channel: 'x', body: 'Candidate-grounded draft',
    })
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ['/api/campaigns/campaign%2F1/posts', 'POST', JSON.stringify({ channel: 'x', body: 'Canonical draft', scheduled_for: null })],
      ['/api/posts/post%2F1', 'PATCH', JSON.stringify({ body: 'Canonical revision' })],
      ['/api/posts/post%2F1/audit', 'GET', undefined],
      ['/api/posts/post%2F1/dispatch', 'POST', JSON.stringify({})],
      ['/api/brands/points%20mafia/editorial-candidates/candidate%2F1/campaign-post', 'POST', JSON.stringify({ campaign_id: null, campaign_name: 'Draft campaign', objective: 'Help readers decide', channel: 'x', body: 'Candidate-grounded draft' })],
    ])
    expect(JSON.stringify(fetchMock.mock.calls)).not.toMatch(/actor|approve|schedule[^d]|send|publish|provider/)
  })
})
