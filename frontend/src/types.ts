export type Severity = 'BREAKING' | 'POTENTIALLY_BREAKING' | 'NON_BREAKING'
export type Decision = 'PROCEED' | 'BLOCK' | 'HUMAN_REVIEW'

export interface SpecSummary {
  valid: boolean
  filename: string
  openapi_version: string | null
  title: string | null
  api_version: string | null
  errors: { message: string; location: string }[]
  stats: { paths?: number; operations?: number; schemas?: number }
}

export interface Change {
  id: string
  rule: string
  severity: Severity
  direction: 'request' | 'response' | 'surface'
  message: string
  method: string | null
  path: string | null
  candidate_path: string | null
  area: string | null
  media_type: string | null
  status: string | null
  param: string[] | null
  segments: string[]
  component: string | null
  before: unknown
  after: unknown
  location: string
}

export interface TestCase {
  id: string
  change_ids: string[]
  kind: string
  title: string
  rationale: string
  predicted: 'incompatible' | 'compatible' | 'uncertain'
  locator: Record<string, unknown>
  payload: unknown
  notes: string[]
  origin: string
}

export interface TestResult {
  test_id: string
  status: 'passed' | 'failed' | 'error'
  observed: 'compatible' | 'incompatible' | null
  verdict: string
  summary: string
  errors: { path: string; message: string }[]
  collateral: { path: string; message: string }[]
  duration_ms: number
}

export interface Reason {
  code: string
  change_ids: string[]
  text: string
}

export interface Recommendation {
  decision: Decision
  reasons: Reason[]
  counts: Record<string, number>
  change_status: Record<string, { tests: string[]; failed: number; passed: number; errors: number; evidence: string }>
  policy: string
  final_approval: string
}

export interface Analysis {
  id: string
  created_at: string
  base: SpecSummary
  candidate: SpecSummary
  changes: Change[]
  tests: TestCase[]
  results: TestResult[]
  recommendation: Recommendation
  timings: Record<string, number>
  mode: string
  sample?: string | null
  investigation?: Investigation | null
  reviews: Review[]
  review_policy: ReviewPolicy
}

export interface ReviewPolicy {
  effective_recommendation: Decision
  source: 'agent' | 'baseline'
  investigation_id: string | null
  must_acknowledge_to_approve: string[]
  min_comment: number
  min_override_comment: number
}

export interface Review {
  id: number
  analysis_id: string
  created_at: string
  reviewer: string
  decision: 'APPROVED' | 'REJECTED'
  comment: string
  system_recommendation: Decision
  agent_recommendation: Decision | null
  investigation_id: string | null
  overrides_recommendation: boolean
}

export interface AnalysisListItem {
  id: string
  created_at: string
  base_filename: string
  candidate_filename: string
  title: string | null
  base_version: string | null
  candidate_version: string | null
  decision: Decision
  agent_decision: Decision | null
  review_decision: 'APPROVED' | 'REJECTED' | null
}

export interface AuditEvent {
  seq: number
  ts: string
  actor: string
  action: string
  analysis_id: string | null
  details: Record<string, unknown>
  prev_hash: string
  hash: string
}

export interface ChainStatus {
  valid: boolean
  events: number
  head?: string
  broken_at?: number
}

export interface DetectionCase {
  case: string
  why: string
  oas: string
  expected_changes: number
  detected: number
  matched: number
  missing: string[]
  unexpected: string[]
  severity_mismatches: string[]
  expected_decision: Decision
  decision: Decision
  tests: number
  pass: boolean
}

export interface AgentCase {
  affected?: string[] | null
  systems?: Record<string, { decision: Decision; affected: string[]; pass: boolean; false_escalation: boolean }>
  name: string
  sample: string
  context: string
  expected: Decision
  baseline: Decision
  agent: Decision
  why: string
  pass: boolean
  escalated: boolean
  trace_steps: number
}

export interface EvaluationReport {
  duration_ms: number
  detection: {
    cases: DetectionCase[]
    metrics: Record<string, number>
    decision_confusion: Record<string, Record<string, number>>
    baselines?: GateBaseline[]
  }
  agent: { cases: AgentCase[]; metrics: Record<string, number | string | null>; comparison?: AgentComparison[] }
}

export interface AgentComparison {
  system: string
  label: string
  decision_accuracy: number
  correct: number
  cases: number
  false_escalations: number
  impact_precision: number | null
  impact_recall: number | null
  impact_tp: number
  impact_fp: number
  impact_fn: number
}

export interface GateBaseline {
  system: string
  label: string
  correct: number
  cases: number
  decision_accuracy: number
  wrong: string[]
}

export interface InvestigationJob {
  id: string
  analysis_id: string
  status: 'running' | 'done' | 'failed'
  error: string | null
  investigation_id: string | null
  elapsed_ms: number
  events: TraceEvent[]
  event_count: number
  context_files: string[]
  target_url: string | null
  investigation?: Investigation
}

export interface LlmCheck {
  configured: boolean
  ok: boolean
  provider?: string
  model?: string
  category?: string | null
  message: string
  latency_ms?: number
  resolved_model?: string
  model_note?: string
}

export interface LlmRuntime {
  provider: string
  endpoint: string
  model: string
  reachable: boolean
  version: string | null
  model_present: boolean | null
  installed_models: string[]
  capabilities: string[] | null
  num_ctx: number
  error: string | null
}

export interface LlmLast {
  check: { ok: boolean; category?: string | null; live: boolean; message?: string; latency_ms?: number; at: string } | null
  selftest: { ok: boolean; live: boolean; at: string
    steps: { id: string; name: string; ok: boolean; skipped?: boolean; category?: string | null; detail?: string }[] } | null
  investigation: { status: 'completed' | 'failed'; mode?: LlmMode | null; live?: boolean; tool_calls?: number
    assessments_by_llm?: number; recommendation?: string; error?: string; at: string } | null
}

export interface LlmStatus {
  configured: boolean
  provider: string | null
  model: string | null
  disabled_by_setting: boolean
  configuration_problem: string | null
  runtime: LlmRuntime | null
  last: LlmLast
}

export interface Sample {
  name: string
  description: string
  context_files: string[]
}

export interface Finding {
  chunk_id: string
  citation?: string
  finding: string
  detail?: string
  quote?: string
  lines?: { line: number; text: string }[]
  operation_call?: { line: number; text: string } | null
  source: 'rules' | 'llm'
  verified: boolean
  verification: string
}

export interface RefinedTest {
  id: string
  kind: 'consumer_expectation' | 'consumer_request_replay' | 'live_http'
  title: string
  rationale: string
  derived_from?: { constraint: string; evidence: string[] }[]
  payload?: unknown
  based_on?: string
}

export interface RefinedResult {
  test_id: string
  status: 'passed' | 'failed' | 'inconclusive' | 'error' | 'timeout' | 'infra_error'
  summary: string
  errors: { path: string; message: string }[]
  collateral?: { path: string; message: string }[]
  request?: Record<string, unknown>
  response?: Record<string, unknown>
  duration_ms: number
}

export interface EvidenceHit {
  id: string
  citation: string
  source: string
  kind: 'context' | 'spec'
  text: string
  score: number
  start_line: number
}

export interface Attempt {
  attempt: number
  strategy: 'focused' | 'broadened'
  hits: string[]
  outcome: string
  verified: number
  discarded: number
}

export interface ToolCallLog {
  round: number
  call_id: string
  tool: string
  arguments: unknown
  result_ids: string[]
  error: string | null
}

export interface AgentRecord {
  tool_calls?: ToolCallLog[]
  validation_problems?: string[]
  cross_check?: { chunk_id: string; finding: string; citation?: string; detail?: string }[]
  attempts?: Attempt[]
  change_id: string
  outcome: string
  impact: string
  mode: 'rules' | 'llm'
  rationale: string
  note: string | null
  findings: Finding[]
  evidence: EvidenceHit[]
  refined_tests: RefinedTest[]
  refined_results: RefinedResult[]
  confirming_tests: string[]
  llm: { model: string; raw: string; live?: boolean } | null
}

export interface TraceEvent {
  seq: number
  node: string
  change_id: string | null
  mode: 'tool' | 'rules' | 'llm'
  summary: string
  detail: Record<string, unknown>
  started_at: string
  duration_ms: number
}

export type LlmMode = 'llm_assisted' | 'partial_fallback' | 'rules_fallback' | 'rules_only' | 'llm_not_needed'

export interface LlmRun {
  mode?: LlmMode
  provider?: string | null
  tool_calls?: number
  live?: boolean
  configured: string | null
  assessments_by_llm: number
  failed_calls: number
  skipped_after_failure: number
  disabled: { category: string; message: string } | null
  errors: { category: string; message: string }[]
}

export interface Investigation {
  llm?: LlmRun
  stop_reason?: string | null
  id: string
  analysis_id: string
  created_at: string
  reasoning: string
  duration_ms: number
  context_files: string[]
  target_url: string | null
  evidence_chunks: number
  plan: { targets: { change_id: string; why: string }[]; skipped: string[] }
  records: AgentRecord[]
  trace: TraceEvent[]
  decision: {
    decision: Decision
    baseline_decision: Decision
    adjustments: { code: string; change_ids: string[]; evidence_ids: string[]; test_ids: string[]; text: string }[]
    outcomes: Record<string, string>
    policy: string
  }
}

export interface ServerConfig {
  version?: string
  llm?: { provider: string | null; model: string | null; disabled_by_setting: boolean
    configuration_problem?: string | null; tool_calling?: boolean; endpoint?: string | null }
  reasoning: string | null
  live_allowed_hosts: string[]
  limits: { spec_bytes: number; context_files: number; context_bytes: number }
}

export interface SelfTestStep {
  id: 'A' | 'B' | 'C'
  name: string
  ok: boolean
  skipped?: boolean
  detail?: string
  category?: string | null
  latency_ms?: number
}

export interface SelfTest {
  configured: boolean
  ok: boolean
  live?: boolean
  provider?: string
  model?: string
  message?: string
  steps: SelfTestStep[]
}
