import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api'
import { DECISION_COPY, ruleLabel } from '../labels'
import { actionLabel, describe, when } from '../format'
import type { Analysis, AuditEvent, ChainStatus, Review } from '../types'
import { SevMark } from './ResultsView'

const NAME_KEY = 'sentinel.reviewer'

function readName() {
  try { return localStorage.getItem(NAME_KEY) ?? '' } catch { return '' }
}
function saveName(v: string) {
  try { localStorage.setItem(NAME_KEY, v) } catch { /* storage unavailable: name just isn't remembered */ }
}


function ReviewHistory({ reviews }: { reviews: Review[] }) {
  if (reviews.length === 0) return null
  const [latest, ...older] = reviews
  return (
    <div className={`decided decided--${latest.decision}`}>
      <p className="decided__head">
        <strong>{latest.decision === 'APPROVED' ? 'Release approved' : 'Release rejected'}</strong> by {latest.reviewer},{' '}
        {when(latest.created_at)}
        {latest.overrides_recommendation && <span className="tag tag--warn">Overrides recommendation</span>}
      </p>
      <p className="decided__comment">“{latest.comment}”</p>
      <p className="muted small">
        Recommendation in force when decided: {latest.agent_recommendation ? 'agent' : 'baseline'}{' '}
        <strong>{DECISION_COPY[latest.agent_recommendation ?? latest.system_recommendation].word}</strong>
        {latest.agent_recommendation && <> (baseline: {DECISION_COPY[latest.system_recommendation].word})</>}.
      </p>
      {older.length > 0 && (
        <details>
          <summary className="small">{older.length} earlier decision{older.length > 1 ? 's' : ''}</summary>
          <ul className="history">
            {older.map((r) => (
              <li key={r.id}>
                <strong>{r.decision === 'APPROVED' ? 'Approved' : 'Rejected'}</strong> by {r.reviewer}, {when(r.created_at)}: “{r.comment}”
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

export function ReviewPanel({ a, onChange, onPick }: { a: Analysis; onChange: (a: Analysis) => void; onPick: (id: string) => void }) {
  const pol = a.review_policy
  const [reviewer, setReviewer] = useState(readName)
  const [decision, setDecision] = useState<'APPROVED' | 'REJECTED' | null>(null)
  const [comment, setComment] = useState('')
  const [ack, setAck] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ text: string; stale?: boolean } | null>(null)
  const [open, setOpen] = useState(a.reviews.length === 0)

  const byId = useMemo(() => new Map(a.changes.map((c) => [c.id, c])), [a.changes])
  const eff = pol.effective_recommendation
  const overridingBlock = decision === 'APPROVED' && eff === 'BLOCK'
  const overridingProceed = decision === 'REJECTED' && eff === 'PROCEED'
  const minLen = overridingBlock ? pol.min_override_comment : pol.min_comment
  const needAck = decision === 'APPROVED' ? pol.must_acknowledge_to_approve : []
  const missingAck = needAck.filter((id) => !ack.has(id))
  const nameOk = /^[\w .@'-]{2,80}$/.test(reviewer.trim())
  const ready = decision !== null && nameOk && comment.trim().length >= minLen && missingAck.length === 0 && !busy

  const toggle = (id: string) => setAck((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!ready || !decision) return
    setBusy(true); setError(null)
    try {
      saveName(reviewer.trim())
      const updated = await api.review(a.id, {
        reviewer: reviewer.trim(), decision, comment: comment.trim(),
        acknowledged_change_ids: [...ack], seen_investigation_id: pol.investigation_id,
      })
      setComment(''); setDecision(null); setAck(new Set()); setOpen(false)
      onChange(updated)
    } catch (err) {
      const stale = err instanceof ApiError && err.status === 409
      setError({ text: err instanceof Error ? err.message : String(err), stale })
    } finally { setBusy(false) }
  }

  const reload = async () => { setError(null); onChange(await api.get(a.id)) }

  return (
    <section className="review" aria-labelledby="review-h">
      <h2 id="review-h">Release decision</h2>
      <p className="muted">
        The {pol.source === 'agent' ? 'investigation agent' : 'deterministic analysis'} recommends{' '}
        <strong className={`word word--${eff}`}>{DECISION_COPY[eff].word}</strong>. That is advice. The release is
        approved or rejected only by the person who signs below, and the decision goes into the audit trail.
      </p>
      <ReviewHistory reviews={a.reviews} />
      {!open ? (
        <button type="button" className="btn btn--quiet" onClick={() => setOpen(true)}>Record a new decision</button>
      ) : (
        <form className="reviewform" onSubmit={submit} noValidate>
          <fieldset className="choice">
            <legend className="field__label">Decision</legend>
            <label className={`choice__opt choice__opt--approve${decision === 'APPROVED' ? ' is-on' : ''}`}>
              <input type="radio" name="decision" checked={decision === 'APPROVED'} onChange={() => setDecision('APPROVED')} />
              Approve release
            </label>
            <label className={`choice__opt choice__opt--reject${decision === 'REJECTED' ? ' is-on' : ''}`}>
              <input type="radio" name="decision" checked={decision === 'REJECTED'} onChange={() => setDecision('REJECTED')} />
              Reject release
            </label>
          </fieldset>

          {overridingBlock && (
            <p className="warnline" role="note">
              You are approving against a <strong>Block</strong> recommendation. It will be recorded as an override and
              needs a rationale of at least {pol.min_override_comment} characters.
            </p>
          )}
          {overridingProceed && (
            <p className="warnline" role="note">You are rejecting a release the system considers safe. It will be recorded as an override.</p>
          )}

          {needAck.length > 0 && (
            <fieldset className="ack">
              <legend className="field__label">
                Acknowledge the {needAck.length} change{needAck.length > 1 ? 's' : ''} this recommendation rests on
              </legend>
              <p className="muted small">
                Approving means you accept each of these.{' '}
                <button type="button" className="linkbtn" onClick={() => setAck(new Set(needAck))}>Tick all</button>
              </p>
              <ul>
                {needAck.map((id) => {
                  const c = byId.get(id)
                  return (
                    <li key={id}>
                      <label className="ack__item">
                        <input type="checkbox" checked={ack.has(id)} onChange={() => toggle(id)} />
                        {c && <SevMark severity={c.severity} />}
                        <span>
                          <span className="ack__rule">{c ? ruleLabel(c.rule) : id}</span>{' '}
                          <button type="button" className="chip" onClick={() => onPick(id)}>{id}</button>
                          {c && <span className="ack__where">{c.location}</span>}
                        </span>
                      </label>
                    </li>
                  )
                })}
              </ul>
            </fieldset>
          )}

          <div className="reviewform__row">
            <label className="field">
              <span className="field__label">Your name</span>
              <input className="text" value={reviewer} onChange={(e) => setReviewer(e.target.value)} maxLength={80}
                autoComplete="name" aria-invalid={reviewer.length > 0 && !nameOk} />
            </label>
            <label className="field field--grow">
              <span className="field__label">Rationale</span>
              <textarea className="text text--area" value={comment} onChange={(e) => setComment(e.target.value)} rows={3}
                maxLength={2000} placeholder="Why is this the right call? Who did you check with?" />
              <span className={`small ${comment.trim().length >= minLen ? 'muted' : 'error'}`}>
                {comment.trim().length}/{minLen} characters minimum
              </span>
            </label>
          </div>

          {error && (
            <p className="error" role="alert">
              {error.text}{' '}
              {error.stale && <button type="button" className="btn btn--quiet" onClick={reload}>Reload analysis</button>}
            </p>
          )}
          <div className="reviewform__actions">
            <button type="submit" className="btn btn--primary" disabled={!ready}>
              {busy ? 'Recording…' : decision === 'REJECTED' ? 'Record rejection' : 'Record approval'}
            </button>
            {a.reviews.length > 0 && <button type="button" className="btn btn--quiet" onClick={() => setOpen(false)}>Cancel</button>}
            {!ready && decision && missingAck.length > 0 && (
              <span className="muted small">{missingAck.length} change{missingAck.length > 1 ? 's' : ''} still to acknowledge</span>
            )}
          </div>
        </form>
      )}
    </section>
  )
}

export function ChainBadge({ chain }: { chain: ChainStatus | null }) {
  if (!chain) return null
  return chain.valid ? (
    <span className="tag tag--ok" title={chain.head ? `Head hash ${chain.head}` : undefined}>
      Hash chain intact · {chain.events} event{chain.events === 1 ? '' : 's'}
    </span>
  ) : (
    <span className="tag tag--bad">Hash chain broken at event #{chain.broken_at}: the log was altered</span>
  )
}

export function AuditTrail({ analysisId, refreshKey }: { analysisId: string; refreshKey: string }) {
  const [events, setEvents] = useState<AuditEvent[] | null>(null)
  const [chain, setChain] = useState<ChainStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    api.audit(analysisId).then((r) => { setEvents(r.events); setChain(r.chain) }).catch((e) => setErr(e.message))
  }, [analysisId, refreshKey])
  return (
    <section className="audittrail" aria-labelledby="audit-h">
      <div className="audittrail__head">
        <h2 id="audit-h">Audit trail</h2>
        <ChainBadge chain={chain} />
      </div>
      {err && <p className="error">{err}</p>}
      {events && (
        <ol className="timeline">
          {[...events].reverse().map((e) => (
            <li key={e.seq}>
              <span className="timeline__when small muted">{when(e.ts)}</span>
              <span><strong>{actionLabel(e.action)}</strong> <span className="muted">by {e.actor}</span></span>
              <span className="timeline__what">{describe(e)}</span>
            </li>
          ))}
        </ol>
      )}
      <p className="muted small"><a href="#/audit">Full audit log</a></p>
    </section>
  )
}
