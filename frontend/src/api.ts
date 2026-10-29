import type {
  Analysis, AnalysisListItem, AuditEvent, ChainStatus, EvaluationReport, Investigation, InvestigationJob, LlmCheck, LlmStatus,
  Sample, SelfTest, ServerConfig, SpecSummary,
} from './types'

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, message: string, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(url, init)
  } catch {
    throw new ApiError(0, 'The API server is not reachable. Start the backend and try again.', null)
  }
  const body = await res.json().catch(() => null)
  if (!res.ok) {
    const detail = body?.detail
    const message = typeof detail === 'string' ? detail : detail?.message ?? `Request failed (${res.status})`
    throw new ApiError(res.status, message, detail)
  }
  return body as T
}

export const api = {
  samples: () => request<Sample[]>('/api/samples'),
  validate: (file: File) => {
    const fd = new FormData()
    fd.append('spec', file)
    return request<SpecSummary>('/api/validate', { method: 'POST', body: fd })
  },
  analyse: (base: File, candidate: File) => {
    const fd = new FormData()
    fd.append('base_spec', base)
    fd.append('candidate_spec', candidate)
    return request<Analysis>('/api/analyses', { method: 'POST', body: fd })
  },
  analyseSample: (name: string) => request<Analysis>(`/api/analyses/sample/${encodeURIComponent(name)}`, { method: 'POST' }),
  get: (id: string) => request<Analysis>(`/api/analyses/${encodeURIComponent(id)}`),
  config: () => request<ServerConfig>('/api/config'),
  investigate: (id: string, files: File[], targetUrl: string, useSampleContext: boolean) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('context', f))
    fd.append('target_url', targetUrl)
    fd.append('use_sample_context', String(useSampleContext))
    return request<Investigation>(`/api/analyses/${encodeURIComponent(id)}/investigate`, { method: 'POST', body: fd })
  },
  startInvestigation: (id: string, files: File[], targetUrl: string, useSampleContext: boolean) => {
    const fd = new FormData()
    files.forEach((f) => fd.append('context', f))
    fd.append('target_url', targetUrl)
    fd.append('use_sample_context', String(useSampleContext))
    return request<{ job_id: string }>(`/api/analyses/${encodeURIComponent(id)}/investigations`, { method: 'POST', body: fd })
  },
  job: (jobId: string, since: number) =>
    request<InvestigationJob>(`/api/investigation-jobs/${encodeURIComponent(jobId)}?since=${since}`),
  health: () => request<{ status: string; version: string }>('/api/health'),
  llmStatus: () => request<LlmStatus>('/api/llm/status'),
  llmCheck: () => request<LlmCheck>('/api/llm/check', { method: 'POST' }),
  llmSelfTest: () => request<SelfTest>('/api/llm/selftest', { method: 'POST' }),
  list: () => request<AnalysisListItem[]>('/api/analyses?limit=200'),
  review: (id: string, body: {
    reviewer: string; decision: 'APPROVED' | 'REJECTED'; comment: string
    acknowledged_change_ids: string[]; seen_investigation_id: string | null
  }) => request<Analysis>(`/api/analyses/${encodeURIComponent(id)}/reviews`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }),
  audit: (analysisId?: string) =>
    request<{ events: AuditEvent[]; chain: ChainStatus }>(
      `/api/audit${analysisId ? `?analysis_id=${encodeURIComponent(analysisId)}` : '?limit=500'}`),
  verifyAudit: () => request<ChainStatus>('/api/audit/verify'),
  evaluation: (refresh = false) => request<EvaluationReport>(`/api/evaluation${refresh ? '?refresh=true' : ''}`),
}
