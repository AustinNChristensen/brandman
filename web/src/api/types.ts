// Shapes mirror app/main.py responses. Keep in sync with the backend; the
// dashboard never invents fields the API does not return.

export interface Brand {
  id: string
  slug: string
  name: string
  mission: string
  voice: string
  compliance_rules: string
  approval_policy: 'human_approval_required' | 'standing_approval'
  created_at: string
  updated_at: string
}

export interface Persona { id: string; brand_id: string; name: string; audience: string; angles: string[]; created_at: string }
export interface Source { id: string; brand_id: string; title: string; url: string | null; source_type: string; body_summary: string | null; lifecycle_state: string; scheduled_for: string | null; external_source_id: string | null; created_at: string }
export interface Campaign { id: string; brand_id: string; source_id: string | null; name: string; objective: string; status: string; created_at: string }
export interface CampaignPost {
  id: string; campaign_id: string; brand_id: string; candidate_id: string | null
  created_by: string | null; channel: string; body: string; status: 'draft' | string
  scheduled_for: string | null; external_post_id: string | null; revision: number
  created_at: string; updated_at: string
}
export interface CampaignPostAudit {
  sequence: number; post_id: string; action: string; actor: string; revision: number
  detail: Record<string, unknown>; at: string
}

export interface OperatorContentProposal {
  id: string; brand_id: string; command: string; status: 'previewed' | 'confirmed'
  evidence_fingerprint: string; guideline_id: string; guideline_version_id: string
  guideline_fingerprint: string; created_by: string; created_at: string
  confirmed_by: string | null; confirmed_at: string | null
  evidence: { kind: 'source' | 'candidate'; id: string; title: string; summary: string; url?: string | null }[]
  guideline: { id: string; version_id: string; content_fingerprint: string; instructions?: string; rules?: Record<string, unknown>; version?: number; name?: string }
  preview: {
    campaign: { name: string; objective: string; status: 'draft' }
    newsletter: NewsletterRevision
    x_draft: { body: string; status: 'draft' }
  }
  result: { campaign_id: string; newsletter_issue_id: string; x_post_id: string } | null
}

export interface BrandGuidelineVersion {
  id: string; guideline_id: string; version: number; instructions: string
  rules: Record<string, unknown>; source_ref: string | null; change_reason: string
  created_by: string; content_fingerprint: string; created_at: string
}
export interface BrandGuideline {
  id: string; brand_id: string; content_type: string; channel: string; name: string
  status: 'draft' | 'active' | 'archived'; active_version_id: string | null
  created_at: string; updated_at: string; versions: BrandGuidelineVersion[]
  active_version: BrandGuidelineVersion | null
}
export interface BrandGuidelineAudit {
  sequence: number; guideline_id: string; version_id: string | null; action: string
  actor: string; reason: string; details: Record<string, unknown>; at: string
}

export interface BrandContext extends Brand {
  personas: Persona[]
  sources: Source[]
  campaigns: Campaign[]
  accepted_learnings: Learning[]
}

export type DispatchStatus = 'draft' | 'awaiting_approval' | 'approved' | 'queued' | 'published' | 'measured' | 'rejected' | 'cancelled'

export interface ApprovalScope {
  brand_id: string
  account_ref: string
  resource_type: 'dispatch_item' | 'engagement_action' | 'newsletter_issue'
  resource_id: string
  action_type: string
  destination: string
  intended_schedule: string | null
  revision: number
  campaign_id: string | null
  asset_membership_id: string | null
  material_fingerprint: string
  review_token: string
}

export interface DispatchItem {
  id: string
  brand_id: string
  canonical_post_id: string | null
  connector: string
  payload: { body?: string; [key: string]: unknown }
  status: DispatchStatus
  revision: number
  approval: { approver: string; revision: number; approved_at: string } | null
  external_id: string | null
  external_url: string | null
  attempt_count: number
  last_error: string | null
  updated_at: string
  approval_scope?: ApprovalScope
}

export interface DispatchAudit { item_id: string; action: string; actor: string; at: string; revision: number; detail: string | null }
export interface DispatchValidation { connector: string; valid: boolean; effective_length?: number; errors: string[] }

export interface EditorialCandidate {
  id: string
  brand_id: string
  duplicate_identity: string
  title: string
  summary: string
  recommended_treatment: string
  score: number
  scoring_inputs: Record<string, number>
  rationale: string[]
  supporting_sources: { source_id?: string; url?: string; title?: string; [key: string]: unknown }[]
  publisher_name: string
  cluster_key: string
  intelligence: Record<string, unknown>
  status: 'open' | 'selected' | 'abandoned' | 'archived'
  created_at: string
  updated_at: string
}

export interface ProductFeedbackComment { id: string; feedback_id: string; actor: string; body: string; created_at: string }
export interface ProductFeedbackHistory {
  sequence: number; feedback_id: string; action: string; actor: string; at: string
  from_status: string | null; to_status: string | null; details: Record<string, unknown>
}
export interface ProductFeedback {
  id: string; brand_id: string; reporter: string; summary: string; details: string
  status: 'open' | 'in_progress' | 'resolved' | 'verified'
  component: string; severity: 'low' | 'medium' | 'high' | 'critical'
  fingerprint: string | null; reproduction: string; expected_behavior: string
  actual_behavior: string; workaround: string; related_ids: string[]
  first_seen_at: string; last_seen_at: string; occurrence_count: number; updated_at: string
  assignee: string | null; implementation_links: string[]; implementation_notes: string
  resolution_evidence: string; resolved_by: string | null; resolved_at: string | null
  verified_by: string | null; verified_at: string | null
  comments?: ProductFeedbackComment[]; history?: ProductFeedbackHistory[]
  implementation_matches?: { id: string; component: string; reason: string; implementation_links: string[]; matched_at: string }[]
}

export type NewsletterLifecycle = 'idea' | 'outline' | 'draft' | 'fact_checked' | 'approved' | 'exported' | 'scheduled' | 'published' | 'abandoned' | 'archived'
export const NEWSLETTER_LADDER: NewsletterLifecycle[] = ['idea', 'outline', 'draft', 'fact_checked', 'approved', 'exported', 'scheduled', 'published']

export interface NewsletterClaim { id?: string; claim_id?: string; text?: string; statement?: string; citations?: { source_id?: string; url?: string; quote?: string }[]; [key: string]: unknown }
export interface NewsletterRevision {
  id: string; issue_id: string; revision: number
  editorial_thesis: string; target_reader: string; intended_outcome: string
  working_title: string; final_title: string; subject: string; preview_text: string
  sections: { heading?: string; body?: string }[]
  cta: { label?: string; url?: string }
  seo: { title?: string; description?: string }
  content_basis: { kind?: string; statement?: string }
  claims: NewsletterClaim[]
  source_provenance: { source_id?: string; url?: string; title?: string; [key: string]: unknown }[]
  change_note: string | null; created_by: string; created_at: string
}
export interface NewsletterIssue {
  id: string; brand_id: string; candidate_id: string | null
  lifecycle: NewsletterLifecycle; current_revision: number
  approved_revision: number | null; approved_by: string | null; approved_at: string | null
  beehiiv_external_id: string | null; beehiiv_preview_url: string | null
  scheduled_for: string | null; published_at: string | null
  created_at: string; updated_at: string
  content: NewsletterRevision
  approval_valid: boolean
  governance: { reviewable: boolean; fact_check_valid: boolean; next_safe_action: string; blockers: { code: string; message: string }[] }
  approval_scope?: ApprovalScope
}
export interface FactCheck { id: string; issue_id: string; revision: number; reviewer: string; verdicts: { claim_id: string; verified: boolean; notes?: string }[]; notes: string | null; content_fingerprint: string; passed: boolean; created_at: string }
export interface LifecycleEvent { id: string; entity_type: string; entity_id: string; action: string; from_state: string | null; to_state: string | null; actor: string; reason: string | null; revision: number | null; created_at: string }

export interface CalendarItem {
  item_type: 'source' | 'post' | 'newsletter'
  id: string
  external_source_id: string | null
  title: string
  channel: string
  status: string
  scheduled_for: string | null
  url: string | null
  body_summary: string | null
}

export interface WorkflowStage { key: string; label: string; status: 'done' | 'waiting' | 'current' }
export interface OperatorWorkflow {
  schema_version: number
  focus: { candidate_id: string | null; issue_id: string | null; issue_revision: number | null; package_id: string | null; campaign_id: string | null }
  stages: WorkflowStage[]
  completed_steps: number; total_steps: number; progress_percent: number
  next_action: { code: string; text: string; target: string }
  assisted_measurement: { pending: number; completed: number; failed: number; read_only: boolean }
  safety: string
}

export interface UsageTotal { provider: string; billable_category: string; currency: string | null; unit_name: string; requests: number; units: string; estimated_cost: string | null }
export interface ProviderUsage { brand_id: string; generated_at: string; request_count: number; unpriced_request_count: number; totals: UsageTotal[]; breakdowns: UsageTotal[] }

export interface Learning {
  id: string; brand_id: string; hypothesis: string; evidence: string; proposed_change: string
  status: 'proposed' | 'testing' | 'accepted' | 'rejected' | 'superseded'
  created_at: string; reviewed_at: string | null; review_at: string | null; active: boolean
  accepted_at: string | null; disabled_at: string | null; supersedes_id: string | null
  scope: Record<string, unknown>; evidence_for: unknown[]; evidence_against: unknown[]; effect: Record<string, unknown>; uncertainty: Record<string, unknown>
}

export interface LearningAudit {
  sequence: number; learning_id: string; brand_id: string; action: string; actor: string
  details: Record<string, unknown>; at: string
}

export interface ExperimentVariant {
  id: string; experiment_id: string; variant_key: string; post_id: string; rationale: string
  body: string; post_status: string; scheduled_for: string | null; external_post_id: string | null
}
export interface ExperimentRecommendation {
  id: string; experiment_id: string; measurement_window_id: string | null
  status: 'recommended' | 'accepted' | 'tie' | 'insufficient_evidence'
  winner_variant_id: string | null; metric: string; rationale: string
  evidence: { variant_id: string; variant_key: string; post_id: string; observation_count: number; impressions: number; metric_value: number }[]
  accepted_by: string | null; accepted_at: string | null; learning_id: string | null; created_at: string
}
export interface ExperimentWindow {
  id: string; experiment_id: string; window_key: string; metric: string
  opens_at: string; closes_at: string; evaluate_at: string; late_evidence_until: string
  status: string; evidence_state: string; collection_round: number
  recommendation_id: string | null; has_late_evidence: boolean
}
export interface ContentExperiment {
  id: string; brand_id: string; campaign_id: string; source_id: string; hypothesis: string
  metric: 'impressions' | 'clicks' | 'engagements' | 'conversions'; guardrails: Record<string, number>
  status: 'active' | 'completed'; accepted_recommendation_id: string | null
  variants: ExperimentVariant[]; recommendations: ExperimentRecommendation[]
  measurement_windows: ExperimentWindow[]; created_at: string; updated_at: string
}

export interface EngagementOpportunity {
  id: string; brand_id: string; connector_account_id: string; event_identity: string
  external_post_id: string; opportunity_type: 'mention' | 'reply' | 'search' | 'target_account'
  text: string; author: { id?: string; name?: string; username?: string; verified?: boolean }
  thread_context: { external_url?: string; parent_context?: { id?: string; text?: string }[]; [key: string]: unknown }
  source_query: string | null; target_user_id: string | null; ranking_score: number
  ranking_reasons: string[]; state: 'new' | 'drafted' | 'awaiting_approval' | 'acted_on' | 'dismissed' | 'stale' | 'needs_attention'
  requires_approval: true; material_fingerprint: string; dispatch_item_id: string | null
  action_type: 'reply' | 'like' | 'follow' | null; result: Record<string, unknown> | null
  first_seen_at: string; last_seen_at: string; updated_at: string; resurfaced_count: number
}
export interface EngagementHistory {
  id: string; opportunity_id: string; action: string; actor: string
  detail: Record<string, unknown>; created_at: string
}
export interface EngagementActionResult {
  opportunity: EngagementOpportunity; dispatch_item: DispatchItem
}

export interface ScorecardGoal { metric: string; current: number; target: number; remaining: number; required_daily_change: number; trajectory_status: string; trajectory_status_text: string }
export interface Scorecard { mission_name: string; date: string; overall_status: string; goals: ScorecardGoal[]; approvals_waiting: unknown[]; next_priorities: unknown[] }

export interface OrchestrationStatus { as_of: string; schedules: { total: number; enabled: number; due: number }; pending_decisions: number; latest_tick: unknown }
export interface BrandSettingsAudit { sequence: number; brand_id: string; actor: string; reason: string; before: Pick<Brand, 'mission' | 'voice' | 'compliance_rules' | 'approval_policy'>; after: Pick<Brand, 'mission' | 'voice' | 'compliance_rules' | 'approval_policy'>; at: string }
export interface ProviderRateCard { id: string; brand_id: string; version: string; provider: string; method: string; endpoint_pattern: string; billable_category: string; unit_name: string; unit_price: string; currency: string; effective_at: string; configured_by: string; created_at: string }
export interface OrchestrationSchedule { id: string; schedule_key: string; brand_id: string; connector_account_id: string | null; name: string; action_type: string; interval_seconds: number; enabled: boolean; next_run_at: string; last_due_at: string | null; payload: Record<string, unknown>; created_at: string; updated_at: string }
export interface BrandSettings { brand: Brand; audit: BrandSettingsAudit[]; rate_cards: ProviderRateCard[]; orchestration: OrchestrationStatus; schedules: OrchestrationSchedule[] }

export interface ReadinessCheck {
  id: string; label: string; status: 'ready' | 'blocked' | 'not_configured' | string
  code_ready: boolean; configured: boolean; healthy: boolean
  required_for_live?: boolean; account_connected?: boolean | null; account_id?: string | null
  missing_scopes?: string[]; execution_mode?: string; api_connected?: boolean
  detail: string; actions: string[]
  provider_receipts_proven?: string[]; missing_provider_receipts?: string[]
}
export interface LiveReadiness {
  brand: Pick<Brand, 'id' | 'slug' | 'name'>; generated_at: string; ready: boolean
  summary: { checks_total: number; checks_ready: number; required_checks_total: number
    required_checks_ready: number; code_ready_percent: number; live_ready_percent: number
    connector_accounts_ready: number; connector_accounts_total: number }
  checks: ReadinessCheck[]
}
export interface ExecutionControls { controls: ExecutionControl[]; audit: unknown[] }

export interface ExecutionDestination {
  id: string
  connector_type?: string
  account_key?: string
  display_name?: string
  username?: string | null
  mode?: string
  [key: string]: unknown
}

export interface ExecutionTask {
  id: string
  brand_id: string
  provider: 'beehiiv' | 'x'
  resource_type: string
  resource_id: string
  revision: number
  connector_account_id: string | null
  execution_payload: Record<string, unknown>
  material_fingerprint: string
  status: 'pending' | 'claimed' | 'needs_attention' | 'completed' | 'stale'
  claimed_by: string | null
  claim_expires_at: string | null
  receipt_external_id: string | null
  receipt_external_url: string | null
  receipt_status: string | null
  receipt_recorded_at: string | null
  external_action_started_at: string | null
  destination_options: ExecutionDestination[]
  destination_binding_required: boolean
  action_boundary: 'not_started' | 'provider_action_started' | 'completed'
  confirmation_current: boolean
  public_action_confirmation_expires_at: string | null
  receipt_requirements: { status: 'draft' | 'posted'; external_id: string; external_url: string }
  operator_next_action: { code: string; text: string }
}

export interface ExecutionAgent {
  brand_id: string
  agent_id: string
  channel: string
  enabled: boolean
  last_heartbeat_at: string | null
  heartbeat_age_seconds?: number | null
  ready_to_claim?: boolean
}

export interface AggregatePullTask {
  id: string
  status: string
  scheduled_for?: string
  last_failure_code?: string | null
  window_start?: string
  window_end?: string
  [key: string]: unknown
}

export interface ExecutionConsole {
  schema_version: number
  tasks: ExecutionTask[]
  execution_agents: ExecutionAgent[]
  beehiiv_aggregate_pulls: AggregatePullTask[]
  safety: {
    provider_write_performed: boolean
    claim_grants_approval: boolean
    receipt_reconciles_existing_result: boolean
    subscriber_data_allowed: boolean
  }
}

export interface ExecutionControl {
  brand_id: string
  provider: 'all' | 'beehiiv' | 'x'
  enabled: boolean
  effective_enabled: boolean
  updated_by: string | null
  updated_at: string | null
}

export interface ExecutionControlAudit {
  sequence: number
  brand_id: string
  provider: 'all' | 'beehiiv' | 'x'
  enabled: boolean
  actor: string
  at: string
}

export interface ExecutionAudit {
  sequence: number
  task_id: string
  action: string
  actor: string
  at: string
  detail: Record<string, unknown>
}

export interface ExecutionClaim extends ExecutionTask { claim_token: string }

export type ConnectorProvider = 'x' | 'beehiiv' | 'website' | 'rss'
export interface ConnectionLane {
  lane: string; provider: 'x' | 'beehiiv' | 'website'; title: string; purpose: string
  scopes: string[]; capabilities: string[]
  credential_inputs: { key: string; label: string; secret: boolean }[]
  can_read: boolean; can_write: boolean; write_boundary: string
}
export interface ConnectionOnboarding {
  encryption: { ready: boolean; required_for: string; operator_action: string | null }
  modes: { mode: 'assisted' | 'standalone'; title: string; available_now: boolean; credentials_stored: boolean; best_for: string; tradeoff: string }[]
  providers: Record<'x' | 'beehiiv', { authentication: string; availability: string; lanes: ConnectionLane[] }>
  pricing: { source: string; vendor_prices_bundled: boolean; explanation: string }
}
export interface ConnectorAccount {
  id: string; brand_id: string; connector_type: ConnectorProvider; account_key: string
  display_name: string; status: string; scopes: string[]; capabilities: string[]
  configuration: Record<string, unknown>; health_checked_at?: string | null; last_error?: string | null
}
export interface ConnectionMetadata {
  id: string; provider: 'x' | 'beehiiv' | 'website'; account_id: string
  display_name: string; status: string; required_scopes: string[]; granted_scopes: string[]
  missing_scopes: string[]; excessive_scopes: string[]; scope_status: string
  has_credentials: boolean; credential_revision: number; health_checked_at: string | null
  last_error_code: string | null; reconnect_required: boolean; created_at: string; updated_at: string
}
export interface ConnectorHealthCheck {
  id: string; brand_id: string; connector_account_id: string; connector_type: string
  status: string; provider_responded: boolean | number; response_status: number | null
  error_code: string | null; requested_at: string; completed_at: string | null
}

export interface CampaignMembership {
  id: string; campaign_id: string; asset_type: string; asset_id: string; channel: string
  role: 'anchor' | 'touchpoint' | 'supporting'; sequence: number; phase: string; active: number | boolean
  attribution_primary: number | boolean; flight_name: string; notes: string
}
export interface CampaignRelationship { id: string; from_membership_id: string; to_membership_id: string; relationship_type: string; notes: string }
export interface CampaignFlight { id: string; name: string; starts_at: string; ends_at: string }
export interface CampaignGraph extends Campaign {
  memberships: CampaignMembership[]; relationships: CampaignRelationship[]; flights: CampaignFlight[]
  primary_anchor: CampaignMembership | null
}
export interface CampaignTemplate {
  template_key: string; name: string; current_version: number
  contract: { questions: { id: string; label: string; required: boolean }[]; recipe: Record<string, unknown>; guardrails: { hard: string[] } }
}
export interface CampaignMeasurement {
  campaign_id: string; assets: Record<string, { membership: CampaignMembership; observations: { id: string; observed_at: string; native_metrics: Record<string, number>; conversions: number; revenue_cents: number; attribution_confidence: string }[] }>
  aggregate: Record<string, unknown>; cross_channel_rollup?: { clicks: number; conversions: number; revenue_cents: number; cross_channel_ctr: null; reason: string; reach_not_summed: Record<string, Record<string, number>> }
  channels?: Record<string, { native_totals: Record<string, number>; rates: Record<string, { value: number | null; numerator_metric: string; denominator_metric: string }> }>
  conversion_confidence?: string; deduplication?: { strategy: string; unique_records: number }; [key: string]: unknown
}
export interface CampaignAudit { sequence?: number; action: string; actor: string; reason: string; at: string; [key: string]: unknown }
export interface DistributionPackage { id: string; issue_id: string; campaign_id: string; status: string; web?: { url?: string; tracked_url?: string }; artifacts?: unknown[]; [key: string]: unknown }

export interface ThirdPartySource {
  connector_account_id: string; brand_id: string; publisher_name: string; homepage_url: string | null
  feed_url: string; feed_format: string; content_policy: string; polling_interval_seconds: number
  enabled: boolean; connector_status: string; last_error: string | null; health_checked_at: string | null
  running_sync_jobs: number; schedule: { enabled: boolean; next_run_at: string; last_run_at?: string | null } | null
}
export interface CanonicalRevalidation {
  id: string; source_id: string; connector_event_id: string; observed_at: string
  canonical_url: string; title: string | null; summary: string | null; status: string
  evidence_scope: 'canonical_metadata_only'; semantic_fact_check: false; [key: string]: unknown
}
export interface AssistedBeehiivPull {
  id: string; status: string; scheduled_for: string; observed_at?: string | null
  posts_received?: number; post_measurements_received?: number; campaign_metrics_recorded?: number
  measured_campaign_ids?: string[]; allowed_data: string; forbidden_actions: string[]; read_only: true
}
export interface PerformanceRecord {
  id: string; brand_id: string; post_id: string | null; source_id: string | null; channel: string
  observed_at: string; impressions: number; clicks: number; engagements: number
  conversions: number; revenue_cents: number; notes: string
}
export interface PerformancePlan {
  status: string; scope?: Record<string, unknown>; estimate?: Record<string, number>
  bounded_prior?: { score_adjustment_points?: number; unfatigued_adjustment_points?: number; [key: string]: unknown }
  evidence?: { included_count?: number; [key: string]: unknown }
  repetition_guard?: { matching_campaigns_last_30_days?: number; rule?: string; [key: string]: unknown }
  policy?: { role?: string; protected_invariants?: string[]; [key: string]: unknown }; [key: string]: unknown
}
export interface TrackedLink {
  id: string; campaign_id: string; artifact_id: string; cta_id: string
  source: string; medium: string; destination: string; tracked_url: string; created_at: string
}
export interface MorningPlan {
  kind: string; date: string; mission_name?: string; goals: ScorecardGoal[]
  actions?: { title: string; reason: string; owner?: string; due_window?: string; blocker?: string | null; [key: string]: unknown }[]
  [key: string]: unknown
}

export interface PublishingWindow {
  weekday: number
  start: string
  end: string
}

export interface PublishingPlanItem {
  item_type: 'post' | 'newsletter'
  item_id: string
  channel: string
  status: string
  title: string
  initiative_id: string
  planned_for: string | null
  pinned: boolean
  locked: boolean
}

export interface PublishingPlan {
  brand_id: string
  settings: {
    timezone: string
    windows: PublishingWindow[]
    cadence_minutes: Record<string, number>
    updated_by: string | null
    updated_at: string | null
  }
  initiatives: { id: string; items: PublishingPlanItem[] }[]
  safety: { planning_only: boolean; provider_write_performed: boolean; approval_granted: boolean }
}

export interface PublishingReflowChange {
  item_type: 'post' | 'newsletter'
  item_id: string
  initiative_id: string
  channel: string
  before: string | null
  after: string
}

export interface PublishingReflowPreview {
  id: string
  snapshot_fingerprint: string
  changes: PublishingReflowChange[]
  planning_only: boolean
}

export interface PublishingReflowCommit {
  id: string
  brand_id: string
  preview_id: string
  committed_by: string
  committed_at: string
  undone_by: string | null
  undone_at: string | null
  before: unknown[]
  changes: PublishingReflowChange[]
}
