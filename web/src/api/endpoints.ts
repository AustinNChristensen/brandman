import { api, enc } from './client'
import type {
  Brand, BrandContext, BrandSettings, CalendarItem, DispatchAudit, DispatchItem, DispatchValidation, FactCheck, Source,
  Learning, LearningAudit, ContentExperiment, EngagementOpportunity, EngagementHistory, EngagementActionResult, LifecycleEvent, NewsletterIssue, NewsletterRevision, OperatorWorkflow, OrchestrationStatus,
  ProviderUsage, Scorecard, LiveReadiness, ExecutionAgent,
  ExecutionAudit, ExecutionClaim, ExecutionConsole, ExecutionControl,
  ConnectionMetadata, ConnectionOnboarding, ConnectorAccount, ConnectorHealthCheck, ConnectionLane,
  Campaign, CampaignAudit, CampaignGraph, CampaignMeasurement, CampaignPost, CampaignPostAudit, CampaignTemplate, DistributionPackage,
  AssistedBeehiivPull, CanonicalRevalidation, MorningPlan, PerformancePlan, PerformanceRecord, ThirdPartySource, TrackedLink,
  PublishingPlan, PublishingPlanItem, PublishingReflowCommit, PublishingReflowPreview, PublishingWindow,
  EditorialCandidate,
  ProductFeedback, ProductFeedbackComment, ProductFeedbackHistory,
  BrandGuideline, BrandGuidelineAudit, OperatorContentProposal,
} from './types'

export const brands = {
  list: () => api.get<Brand[]>('/api/brands'),
  context: (slug: string) => api.get<BrandContext>(`/api/brands/${enc(slug)}/context`),
  calendar: (slug: string) => api.get<CalendarItem[]>(`/api/brands/${enc(slug)}/calendar`),
  workflow: (slug: string) => api.get<OperatorWorkflow>(`/api/brands/${enc(slug)}/operator-workflow`),
  usage: (slug: string) => api.get<ProviderUsage>(`/api/brands/${enc(slug)}/provider-usage`),
  scorecard: (slug: string) => api.get<Scorecard>(`/api/brands/${enc(slug)}/mission/scorecard`),
  morningPlan: (slug: string) => api.get<MorningPlan>(`/api/brands/${enc(slug)}/mission/morning-plan`),
  recordKpi: (slug: string, body: { metric: 'x_followers' | 'active_beehiiv_subscribers'; value: number; observed_at: string; source: 'manual'; verification_note: string }) => api.post(`/api/brands/${enc(slug)}/mission/kpis`, body),
  learnings: (slug: string) => api.get<Learning[]>(`/api/brands/${enc(slug)}/learnings`),
  activeGuideline: (slug: string, contentType: string, channel: string) =>
    api.get<Record<string, unknown>>(`/api/brands/${enc(slug)}/guidelines/active?content_type=${enc(contentType)}&channel=${enc(channel)}`),
}

export const learningLab = {
  list: (slug: string) => api.get<Learning[]>(`/api/brands/${enc(slug)}/learnings`),
  propose: (slug: string, body: {
    hypothesis: string; evidence: string; proposed_change: string; scope: Record<string, unknown>
    effect?: Record<string, unknown>; uncertainty?: Record<string, unknown>; review_at?: string | null
  }) => api.post<Learning>(`/api/brands/${enc(slug)}/learnings`, body),
  transition: (slug: string, learningId: string, action: 'testing' | 'accept' | 'reject' | 'supersede' | 'disable' | 'enable', reason?: string) =>
    api.post<Learning>(`/api/brands/${enc(slug)}/learnings/${enc(learningId)}/${action}`, reason ? { reason } : {}),
  audit: (slug: string, learningId: string) =>
    api.get<LearningAudit[]>(`/api/brands/${enc(slug)}/learnings/${enc(learningId)}/audit`),
  experiments: (slug: string) => api.get<ContentExperiment[]>(`/api/brands/${enc(slug)}/experiments`),
  experiment: (slug: string, experimentId: string) =>
    api.get<ContentExperiment>(`/api/brands/${enc(slug)}/experiments/${enc(experimentId)}`),
  draftExperiment: (slug: string, body: {
    campaign_id: string; hypothesis: string; metric: ContentExperiment['metric']
    guardrails: Record<string, number>; measurement_windows: Record<string, unknown>[]
  }) => api.post<ContentExperiment>(`/api/brands/${enc(slug)}/experiments`, body),
  recommend: (slug: string, experimentId: string) =>
    api.post<ContentExperiment['recommendations'][number]>(`/api/brands/${enc(slug)}/experiments/${enc(experimentId)}/recommendations`, {}),
  acceptRecommendation: (slug: string, experimentId: string, recommendationId: string) =>
    api.post<ContentExperiment>(`/api/brands/${enc(slug)}/experiments/${enc(experimentId)}/recommendations/${enc(recommendationId)}/accept`, {}),
}

export const engagement = {
  list: (slug: string, state?: EngagementOpportunity['state']) =>
    api.get<EngagementOpportunity[]>(`/api/brands/${enc(slug)}/engagement${state ? `?state=${enc(state)}` : ''}`),
  get: (slug: string, opportunityId: string) =>
    api.get<EngagementOpportunity>(`/api/brands/${enc(slug)}/engagement/${enc(opportunityId)}`),
  history: (slug: string, opportunityId: string) =>
    api.get<EngagementHistory[]>(`/api/brands/${enc(slug)}/engagement/${enc(opportunityId)}/history`),
  draft: (slug: string, opportunityId: string, action_type: 'reply' | 'like' | 'follow', text?: string) =>
    api.post<EngagementActionResult>(`/api/brands/${enc(slug)}/engagement/${enc(opportunityId)}/draft-action`, {
      action_type, ...(text ? { text } : {}),
    }),
  submit: (slug: string, opportunityId: string) =>
    api.post<EngagementActionResult>(`/api/brands/${enc(slug)}/engagement/${enc(opportunityId)}/submit-action`, {}),
  dismiss: (slug: string, opportunityId: string, reason: string) =>
    api.post<EngagementOpportunity>(`/api/brands/${enc(slug)}/engagement/${enc(opportunityId)}/dismiss`, { reason }),
}

export const guidelines = {
  list: (slug: string, includeArchived = false) =>
    api.get<BrandGuideline[]>(`/api/brands/${enc(slug)}/guidelines${includeArchived ? '?include_archived=true' : ''}`),
  audit: (guidelineId: string) =>
    api.get<BrandGuidelineAudit[]>(`/api/brand-guidelines/${enc(guidelineId)}/audit`),
  create: (slug: string, body: { content_type: string; channel: string; name: string; instructions: string; rules: Record<string, unknown>; reason: string; source_ref?: string | null; activate: boolean }) =>
    api.post<BrandGuideline>(`/api/brands/${enc(slug)}/guidelines`, body),
  createVersion: (guidelineId: string, body: { instructions: string; rules: Record<string, unknown>; reason: string; source_ref?: string | null }) =>
    api.post<BrandGuideline>(`/api/brand-guidelines/${enc(guidelineId)}/versions`, body),
  activate: (guidelineId: string, version: number, reason: string) =>
    api.post<BrandGuideline>(`/api/brand-guidelines/${enc(guidelineId)}/versions/${version}/activate`, { reason }),
  archive: (guidelineId: string, reason: string) =>
    api.delete<BrandGuideline>(`/api/brand-guidelines/${enc(guidelineId)}`, { reason }),
}

export const dispatch = {
  list: (slug: string, status?: string) =>
    api.get<DispatchItem[]>(`/api/brands/${enc(slug)}/dispatch-items${status ? `?status=${enc(status)}` : ''}`),
  get: (id: string) => api.get<DispatchItem>(`/api/dispatch-items/${enc(id)}`),
  audit: (id: string) => api.get<DispatchAudit[]>(`/api/dispatch-items/${enc(id)}/audit`),
  validation: (id: string) => api.get<DispatchValidation>(`/api/dispatch-items/${enc(id)}/validation`),
  // Exact-revision approval: the review token must be echoed back verbatim.
  approve: (id: string, revision: number, review_token: string) =>
    api.post<DispatchItem>(`/api/dispatch-items/${enc(id)}/approve`, { revision, review_token }),
  reject: (id: string, revision: number) =>
    api.post<DispatchItem>(`/api/dispatch-items/${enc(id)}/reject`, { revision }),
  create: (slug: string, connector: string, payload: Record<string, unknown>) =>
    api.post<DispatchItem>(`/api/brands/${enc(slug)}/dispatch-items`, { connector, payload }),
  edit: (id: string, payload: Record<string, unknown>) =>
    api.patch<DispatchItem>(`/api/dispatch-items/${enc(id)}`, { payload }),
  submit: (id: string) => api.post<DispatchItem>(`/api/dispatch-items/${enc(id)}/submit`, {}),
}

export const newsletters = {
  list: (slug: string, includeInactive = false) =>
    api.get<NewsletterIssue[]>(`/api/brands/${enc(slug)}/newsletter-issues${includeInactive ? '?include_inactive=true' : ''}`),
  get: (id: string) => api.get<NewsletterIssue>(`/api/newsletter-issues/${enc(id)}`),
  revisions: (id: string) => api.get<NewsletterRevision[]>(`/api/newsletter-issues/${enc(id)}/revisions`),
  history: (id: string) => api.get<LifecycleEvent[]>(`/api/newsletter-issues/${enc(id)}/history`),
  factCheck: (id: string, revision: number) =>
    api.get<FactCheck | null>(`/api/newsletter-issues/${enc(id)}/fact-check?revision=${enc(String(revision))}`),
  recordFactCheck: (id: string, revision: number, verdicts: { claim_id: string; verified: boolean; notes?: string }[], notes?: string) =>
    api.post<NewsletterIssue>(`/api/newsletter-issues/${enc(id)}/fact-check`, { revision, verdicts, notes }),
  transition: (id: string, target: 'outline' | 'draft') =>
    api.post<NewsletterIssue>(`/api/newsletter-issues/${enc(id)}/transition`, { target }),
  approve: (id: string, revision: number, review_token: string) =>
    api.post<NewsletterIssue>(`/api/newsletter-issues/${enc(id)}/approve`, { revision, review_token }),
  reject: (id: string, revision: number, reason: string) =>
    api.post<NewsletterIssue>(`/api/newsletter-issues/${enc(id)}/reject`, { revision, reason }),
  revise: (id: string, changes: Record<string, unknown>, change_note?: string) =>
    api.patch<NewsletterIssue>(`/api/newsletter-issues/${enc(id)}`, { changes, change_note }),
  exportDraft: (id: string) => api.post<unknown>(`/api/newsletter-issues/${enc(id)}/export-draft`, {}),
  policyReview: (id: string, revision: number, checklist: Record<string, boolean>) =>
    api.post<Record<string, unknown>>(`/api/newsletter-issues/${enc(id)}/policy-review`, { revision, checklist }),
  authorizeQuickHit: (id: string, revision: number, reason: string) =>
    api.post<Record<string, unknown>>(`/api/newsletter-issues/${enc(id)}/quick-hit-authorization`, { revision, reason }),
  abandon: (id: string, reason: string) =>
    api.post<NewsletterIssue>(`/api/newsletter-issues/${enc(id)}/abandon`, { reason }),
  archive: (id: string, reason: string) =>
    api.post<NewsletterIssue>(`/api/newsletter-issues/${enc(id)}/archive`, { reason }),
  exportPreview: (id: string) =>
    api.get<Record<string, unknown>>(`/api/newsletter-issues/${enc(id)}/export-preview`),
  exportJobs: (id: string) =>
    api.get<Record<string, unknown>[]>(`/api/newsletter-issues/${enc(id)}/export-jobs`),
  providerReconciliations: (id: string) =>
    api.get<Record<string, unknown>[]>(`/api/newsletter-issues/${enc(id)}/provider-reconciliations`),
  createDistributionPackage: (id: string, payload: Record<string, unknown>) =>
    api.post<Record<string, unknown>>(`/api/newsletter-issues/${enc(id)}/distribution-package`, payload),
  create: (slug: string, content: Record<string, unknown>, candidate_id?: string) =>
    api.post<NewsletterIssue>(`/api/brands/${enc(slug)}/newsletter-issues`, {
      content, candidate_id: candidate_id || null,
    }),
}

export const editorialCandidates = {
  list: (slug: string, includeInactive = false) =>
    api.get<EditorialCandidate[]>(`/api/brands/${enc(slug)}/editorial-candidates${includeInactive ? '?include_inactive=true' : ''}`),
  create: (slug: string, body: {
    title: string; summary: string; dimensions: Record<string, number>;
    recommended_treatment: string; rationale?: string[]; supporting_sources?: Record<string, unknown>[]
  }) => api.post<EditorialCandidate>(`/api/brands/${enc(slug)}/editorial-candidates`, body),
  abandon: (id: string, reason: string) =>
    api.post<EditorialCandidate>(`/api/editorial-candidates/${enc(id)}/abandon`, { reason }),
  archive: (id: string, reason: string) =>
    api.post<EditorialCandidate>(`/api/editorial-candidates/${enc(id)}/archive`, { reason }),
  history: (id: string) => api.get<LifecycleEvent[]>(`/api/editorial-candidates/${enc(id)}/history`),
  promoteToCampaignPost: (slug: string, id: string, body: {
    campaign_id?: string | null; campaign_name?: string; objective?: string; channel: string; body: string
  }) => api.post<{ campaign: Campaign; post: CampaignPost; candidate: EditorialCandidate }>(
    `/api/brands/${enc(slug)}/editorial-candidates/${enc(id)}/campaign-post`, body,
  ),
}

export const campaignPosts = {
  list: (campaignId: string) => api.get<CampaignPost[]>(`/api/campaigns/${enc(campaignId)}/posts`),
  create: (campaignId: string, channel: string, body: string) =>
    api.post<CampaignPost>(`/api/campaigns/${enc(campaignId)}/posts`, { channel, body, scheduled_for: null }),
  edit: (postId: string, body: string) => api.patch<CampaignPost>(`/api/posts/${enc(postId)}`, { body }),
  audit: (postId: string) => api.get<CampaignPostAudit[]>(`/api/posts/${enc(postId)}/audit`),
  createDispatch: (postId: string) => api.post<DispatchItem>(`/api/posts/${enc(postId)}/dispatch`, {}),
}

export const operatorProposals = {
  preview: (slug: string, body: { command: string; source_ids: string[]; candidate_ids: string[] }) =>
    api.post<OperatorContentProposal>(`/api/brands/${enc(slug)}/operator-proposals/preview`, body),
  get: (slug: string, id: string) =>
    api.get<OperatorContentProposal>(`/api/brands/${enc(slug)}/operator-proposals/${enc(id)}`),
  confirm: (slug: string, id: string) =>
    api.post<OperatorContentProposal>(`/api/brands/${enc(slug)}/operator-proposals/${enc(id)}/confirm`, { confirm: true }),
}

export const productFeedback = {
  list: (slug: string) => api.get<ProductFeedback[]>(`/api/brands/${enc(slug)}/product-feedback`),
  get: (slug: string, id: string) => api.get<ProductFeedback>(`/api/brands/${enc(slug)}/product-feedback/${enc(id)}`),
  history: (slug: string, id: string) => api.get<ProductFeedbackHistory[]>(`/api/brands/${enc(slug)}/product-feedback/${enc(id)}/history`),
  report: (slug: string, body: {
    reporter: string; summary: string; details: string; component: string;
    severity: ProductFeedback['severity']; reproduction?: string; expected_behavior?: string;
    actual_behavior?: string; workaround?: string; related_ids?: string[]
  }) => api.post<ProductFeedback>(`/api/brands/${enc(slug)}/product-feedback`, body),
  comment: (slug: string, id: string, body: string) =>
    api.post<ProductFeedbackComment>(`/api/brands/${enc(slug)}/product-feedback/${enc(id)}/comments`, { body }),
  start: (slug: string, id: string, body: { assignee: string; implementation_links?: string[]; implementation_notes?: string }) =>
    api.post<ProductFeedback>(`/api/brands/${enc(slug)}/product-feedback/${enc(id)}/start`, body),
  resolve: (slug: string, id: string, body: { resolution_evidence: string; implementation_links?: string[]; implementation_notes?: string }) =>
    api.post<ProductFeedback>(`/api/brands/${enc(slug)}/product-feedback/${enc(id)}/resolve`, body),
  verify: (slug: string, id: string, evidence: string) =>
    api.post<ProductFeedback>(`/api/brands/${enc(slug)}/product-feedback/${enc(id)}/verify`, { evidence }),
  reopen: (slug: string, id: string, reason: string) =>
    api.post<ProductFeedback>(`/api/brands/${enc(slug)}/product-feedback/${enc(id)}/reopen`, { reason }),
}

export const orchestration = {
  status: () => api.get<OrchestrationStatus>('/api/orchestration/status'),
}

export const settings = {
  get: (slug: string) => api.get<BrandSettings>(`/api/brands/${enc(slug)}/settings`),
  update: (slug: string, body: { mission: string; voice: string; compliance_rules: string; approval_policy: 'human_approval_required' | 'standing_approval'; reason: string }) => api.patch<BrandSettings['brand']>(`/api/brands/${enc(slug)}/settings`, body),
  addRateCard: (slug: string, body: { version: string; operation: 'beehiiv_read' | 'beehiiv_draft' | 'x_owned_read' | 'x_general_read' | 'x_plain_post' | 'x_link_post'; unit_price: string; currency: string; effective_at: string }) => api.post(`/api/brands/${enc(slug)}/provider-rate-cards`, body),
  setSchedule: (slug: string, scheduleKey: string, enabled: boolean) => api.put(`/api/brands/${enc(slug)}/orchestration/schedules/${enc(scheduleKey)}`, { enabled }),
  tick: (slug: string, max_decisions: number) => api.post(`/api/brands/${enc(slug)}/orchestration/tick`, { max_decisions }),
}

export const integrations = {
  onboarding: (slug: string) => api.get<ConnectionOnboarding>(`/api/brands/${enc(slug)}/connection-onboarding`),
  readiness: (slug: string) => api.get<LiveReadiness>(`/api/brands/${enc(slug)}/readiness`),
  connectors: (slug: string) => api.get<ConnectorAccount[]>(`/api/brands/${enc(slug)}/connectors`),
  connections: (slug: string) => api.get<ConnectionMetadata[]>(`/api/brands/${enc(slug)}/connections`),
  health: (slug: string) => api.get<ConnectorHealthCheck[]>(`/api/brands/${enc(slug)}/connector-health-checks`),
  prepareLane: (slug: string, lane: ConnectionLane, accountKey: string, displayName: string) =>
    api.post<ConnectorAccount>(`/api/brands/${enc(slug)}/connectors`, {
      connector_type: lane.provider, account_key: accountKey, display_name: displayName,
      status: 'disconnected', scopes: lane.scopes, capabilities: lane.capabilities,
      configuration: { connection_role: lane.lane, delivery_mode: 'api' },
    }),
  connect: (slug: string, lane: ConnectionLane, accountKey: string, displayName: string, credentials: Record<string, string>) =>
    api.put<ConnectionMetadata>(`/api/brands/${enc(slug)}/connections/${enc(lane.provider)}/${enc(accountKey)}`, {
      display_name: displayName, credentials, required_scopes: lane.scopes, granted_scopes: lane.scopes,
    }),
  disconnect: (slug: string, provider: string, accountKey: string) =>
    api.post<ConnectionMetadata>(`/api/brands/${enc(slug)}/connections/${enc(provider)}/${enc(accountKey)}/disconnect`, {}),
  checkHealth: (slug: string, connectorAccountId?: string) =>
    api.post<ConnectorHealthCheck[]>(`/api/brands/${enc(slug)}/connector-health-checks`, {
      connector_account_id: connectorAccountId ?? null, timeout_seconds: 20,
    }),
}

export const campaignGraphs = {
  list: (slug: string) => api.get<CampaignGraph[]>(`/api/brands/${enc(slug)}/campaign-graphs`),
  get: (id: string) => api.get<CampaignGraph>(`/api/campaigns/${enc(id)}/graph`),
  create: (slug: string, name: string, objective: string, source_id?: string) =>
    api.post<Campaign>(`/api/brands/${enc(slug)}/campaigns`, { name, objective, source_id: source_id || null }),
  attach: (campaignId: string, body: { asset_type: string; asset_id: string; channel: string; role: string; reason: string; sequence?: number; phase?: string; attribution_primary?: boolean; flight_name?: string; notes?: string }) =>
    api.post(`/api/campaigns/${enc(campaignId)}/memberships`, body),
  detach: (membershipId: string, reason: string) => api.post(`/api/campaign-memberships/${enc(membershipId)}/detach`, { reason }),
  anchor: (membershipId: string, reason: string) => api.post(`/api/campaign-memberships/${enc(membershipId)}/anchor`, { reason }),
  reorder: (campaignId: string, membership_ids: string[], reason: string) => api.post(`/api/campaigns/${enc(campaignId)}/memberships/reorder`, { membership_ids, reason }),
  relate: (campaignId: string, from_membership_id: string, to_membership_id: string, relationship_type: string, reason: string, notes = '') => api.post(`/api/campaigns/${enc(campaignId)}/relationships`, { from_membership_id, to_membership_id, relationship_type, reason, notes }),
  addFlight: (campaignId: string, name: string, starts_at: string, ends_at: string, reason: string) => api.post(`/api/campaigns/${enc(campaignId)}/flights`, { name, starts_at, ends_at, reason }),
  measurement: (campaignId: string) => api.get<CampaignMeasurement>(`/api/campaigns/${enc(campaignId)}/measurement`),
  audit: (campaignId: string) => api.get<CampaignAudit[]>(`/api/campaigns/${enc(campaignId)}/graph-audit`),
  metric: (membershipId: string, body: { observed_at: string; native_metrics: Record<string, number>; conversions: number; revenue_cents: number; attribution_confidence: string; idempotency_key: string }) => api.post(`/api/campaign-memberships/${enc(membershipId)}/metrics`, body),
  templates: () => api.get<CampaignTemplate[]>('/api/campaign-templates'),
  preflight: (slug: string, key: string, answers: Record<string, string>) => api.post(`/api/brands/${enc(slug)}/campaign-templates/${enc(key)}/preflight`, { answers, override_reason: null }),
  instantiate: (slug: string, key: string, body: { answers: Record<string, string>; name: string; objective: string; source_id: string; idempotency_key: string; flight_name?: string; flight_start?: string; flight_end?: string }) => api.post<CampaignGraph>(`/api/brands/${enc(slug)}/campaign-templates/${enc(key)}/instantiate`, body),
  packages: (slug: string) => api.get<DistributionPackage[]>(`/api/brands/${enc(slug)}/distribution-packages`),
  bindDestination: (packageId: string, destination_url: string) => api.post<DistributionPackage>(`/api/distribution-packages/${enc(packageId)}/destination`, { destination_url }),
}

export const sourceInputs = {
  create: (slug: string, body: { title: string; source_type: 'manual'; body_summary: string; url?: string; lifecycle_state: 'draft' | 'published' }) => api.post<Source>(`/api/brands/${enc(slug)}/sources`, body),
  thirdParty: (slug: string) => api.get<ThirdPartySource[]>(`/api/brands/${enc(slug)}/third-party-sources`),
  onboard: (slug: string, body: { publisher_name: string; feed_url: string; homepage_url?: string; feed_format: 'auto' | 'rss' | 'atom'; polling_interval_seconds: number; reason: string }) => api.post<ThirdPartySource>(`/api/brands/${enc(slug)}/third-party-sources`, body),
  setEnabled: (connectorId: string, enabled: boolean, reason: string) => api.post<ThirdPartySource>(`/api/third-party-sources/${enc(connectorId)}/${enabled ? 'enable' : 'disable'}`, { reason }),
  canonical: (slug: string) => api.get<CanonicalRevalidation[]>(`/api/brands/${enc(slug)}/canonical-revalidations`),
  revalidate: (slug: string, sourceId: string, idempotency_key: string) => api.post(`/api/brands/${enc(slug)}/sources/${enc(sourceId)}/canonical-revalidations`, { idempotency_key }),
  beehiivPulls: (slug: string) => api.get<AssistedBeehiivPull[]>(`/api/brands/${enc(slug)}/beehiiv-assisted-pulls`),
  requestBeehiivPull: (slug: string, connector_account_id: string, scheduled_for: string) => api.post<AssistedBeehiivPull>(`/api/brands/${enc(slug)}/beehiiv-assisted-pulls`, { connector_account_id, scheduled_for }),
}

export const performance = {
  list: (slug: string) => api.get<PerformanceRecord[]>(`/api/brands/${enc(slug)}/performance`),
  record: (slug: string, body: Omit<PerformanceRecord, 'id' | 'brand_id'>) => api.post<PerformanceRecord>(`/api/brands/${enc(slug)}/performance`, body),
  trackedLinks: (slug: string) => api.get<TrackedLink[]>(`/api/brands/${enc(slug)}/tracked-links`),
  planning: (slug: string, channel: string, topic?: string) => api.get<PerformancePlan>(`/api/brands/${enc(slug)}/performance-planning?stage=portfolio&channel=${enc(channel)}${topic ? `&topic=${enc(topic)}` : ''}`),
  planningAudit: (slug: string) => api.get<Record<string, unknown>[]>(`/api/brands/${enc(slug)}/performance-planning/audit`),
}

export const agents = {
  readiness: (slug: string) => api.get<LiveReadiness>(`/api/brands/${enc(slug)}/readiness`),
  list: (slug: string) => api.get<ExecutionAgent[]>(`/api/brands/${enc(slug)}/execution-agents`),
  configure: (slug: string, agent_id: string, channel: 'browser' | 'mcp', enabled: boolean) =>
    api.post<ExecutionAgent>(`/api/brands/${enc(slug)}/execution-agents`, { agent_id, channel, enabled }),
  heartbeat: (slug: string, agentId: string) =>
    api.post<ExecutionAgent>(`/api/brands/${enc(slug)}/execution-agents/${enc(agentId)}/heartbeat`, {}),
}

export const execution = {
  console: (slug: string) => api.get<ExecutionConsole>(`/api/brands/${enc(slug)}/execution-console`),
  controls: (slug: string) => api.get<{ controls: ExecutionControl[]; audit: unknown[] }>(`/api/brands/${enc(slug)}/execution-controls`),
  setControl: (slug: string, provider: 'all' | 'beehiiv' | 'x', enabled: boolean) =>
    api.put<ExecutionControl>(`/api/brands/${enc(slug)}/execution-controls/${provider}`, { enabled }),
  bindDestination: (taskId: string, connector_account_id: string) =>
    api.put(`/api/execution-tasks/${enc(taskId)}/destination`, { connector_account_id }),
  claim: (taskId: string, actor: string, lease_seconds = 900) =>
    api.post<ExecutionClaim>(`/api/execution-tasks/${enc(taskId)}/claim`, { actor, lease_seconds }),
  confirmX: (taskId: string, expected_revision: number, expected_material_fingerprint: string) =>
    api.post(`/api/execution-tasks/${enc(taskId)}/confirm-public-action`, {
      expected_revision,
      expected_material_fingerprint,
      confirmation_phrase: 'CONFIRM PUBLIC X POST',
      validity_seconds: 300,
    }),
  beehiivManifest: (taskId: string, asset_path: string, existing_draft_id?: string) =>
    api.post<Record<string, unknown>>(`/api/execution-tasks/${enc(taskId)}/beehiiv-private-draft-manifest`, {
      asset_path,
      ...(existing_draft_id ? { existing_draft_id } : {}),
    }),
  begin: (taskId: string, actor: string, claim_token: string) =>
    api.post(`/api/execution-tasks/${enc(taskId)}/begin-external-action`, { actor, claim_token }),
  receipt: (taskId: string, body: {
    claim_token: string; external_id: string; external_url?: string; status: 'draft' | 'posted';
    content_fingerprint?: string; asset_fingerprint?: string
  }) => api.post(`/api/execution-tasks/${enc(taskId)}/receipt`, body),
  audit: (taskId: string) => api.get<ExecutionAudit[]>(`/api/execution-tasks/${enc(taskId)}/audit`),
}

export const publishingPlan = {
  get: (slug: string) =>
    api.get<PublishingPlan>(`/api/brands/${enc(slug)}/publishing-plan`),
  updateSettings: (slug: string, body: {
    timezone: string; windows: PublishingWindow[]; cadence_minutes: Record<string, number>
  }) => api.put<PublishingPlan['settings']>(`/api/brands/${enc(slug)}/publishing-plan/settings`, body),
  updateItem: (slug: string, item: PublishingPlanItem) =>
    api.put<PublishingPlanItem>(
      `/api/brands/${enc(slug)}/publishing-plan/items/${enc(item.item_type)}/${enc(item.item_id)}`,
      {
        initiative_id: item.initiative_id, planned_for: item.planned_for,
        pinned: item.pinned, locked: item.locked,
      },
    ),
  preview: (slug: string, start_at: string) =>
    api.post<PublishingReflowPreview>(`/api/brands/${enc(slug)}/publishing-plan/reflow/preview`, { start_at }),
  commit: (slug: string, preview_id: string) =>
    api.post<PublishingReflowCommit>(`/api/brands/${enc(slug)}/publishing-plan/reflow/commit`, { preview_id }),
  undo: (slug: string, commit_id: string) =>
    api.post<PublishingReflowCommit>(`/api/brands/${enc(slug)}/publishing-plan/reflow/undo`, { commit_id }),
}
