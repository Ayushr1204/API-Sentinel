import type { AuditEvent } from './types'

export function when(ts: string) {
  return new Date(ts).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

const ACTION_LABEL: Record<string, string> = {
  'analysis.created': 'Analysis created',
  'analysis.rejected': 'Upload rejected',
  'investigation.completed': 'Investigation completed',
  'investigation.refused': 'Investigation refused',
  'review.approved': 'Release approved',
  'review.rejected': 'Release rejected',
}

export function actionLabel(a: string) { return ACTION_LABEL[a] ?? a }

export function describe(e: AuditEvent): string {
  const d = e.details as Record<string, unknown>
  switch (e.action) {
    case 'analysis.created':
      return `${d.base} → ${d.candidate}: recommendation ${d.recommendation}`
    case 'investigation.completed':
      return `${d.baseline} → ${d.agent_recommendation} (${d.reasoning}, ${d.trace_steps} steps)`
    case 'review.approved':
    case 'review.rejected':
      return `${d.overrides_recommendation ? 'Override. ' : ''}“${String(d.comment)}”`
    case 'analysis.rejected':
      return `${d.base} / ${d.candidate}: ${Number(d.base_errors) + Number(d.candidate_errors)} validation error(s)`
    default:
      return String(d.reason ?? '')
  }
}
