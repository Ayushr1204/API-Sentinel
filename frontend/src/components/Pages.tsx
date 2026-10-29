import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { actionLabel, describe, when } from '../format'
import type { AnalysisListItem, AuditEvent, ChainStatus, EvaluationReport } from '../types'
import { DecisionWord, Empty, ErrorState, Icon, Loading, PageHeader, Spinner, Stat } from '../ui'
import { ChainBadge } from './ReviewPanel'

function useLoad<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const run = useCallback(() => {
    setBusy(true); setError(null)
    load().then(setData).catch((e) => setError(e.message)).finally(() => setBusy(false))
  }, [load])
  useEffect(() => { run() }, [run])
  return { data, error, busy, run }
}

// ---------------------------------------------------------------- history
type ReviewFilter = 'all' | 'awaiting' | 'APPROVED' | 'REJECTED'

export function HistoryView() {
  const { data, error, run } = useLoad(api.list)
  const [q, setQ] = useState('')
  const [rf, setRf] = useState<ReviewFilter>('all')
  const [oldest, setOldest] = useState(false)
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const out = (data ?? []).filter((r) => (!needle || `${r.title} ${r.base_filename} ${r.candidate_filename} ${r.id}`.toLowerCase().includes(needle))
      && (rf === 'all' || (rf === 'awaiting' ? !r.review_decision : r.review_decision === rf)))
    return oldest ? [...out].reverse() : out
  }, [data, q, rf, oldest])
  return (
    <main className="page">
      <PageHeader title="History" subtitle="Every analysis is stored in the local SQLite database with its recommendations and the human release decision." />
      {error && <ErrorState title="Could not load history" message={error} onRetry={run} />}
      {!data && !error && <Loading label="Loading analyses…" />}
      {data && data.length === 0 && <Empty title="No analyses yet"><a href="#">Run a comparison</a> to start.</Empty>}
      {data && data.length > 0 && (
        <>
          <div className="toolbar">
            <label className="search"><Icon name="search" size={16} />
              <input type="search" placeholder="Search API or file…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search history" />
            </label>
            <label className="select small">Release decision
              <select value={rf} onChange={(e) => setRf(e.target.value as ReviewFilter)}>
                <option value="all">All</option><option value="awaiting">Awaiting</option>
                <option value="APPROVED">Approved</option><option value="REJECTED">Rejected</option>
              </select>
            </label>
            <label className="select small">Sort
              <select value={oldest ? 'old' : 'new'} onChange={(e) => setOldest(e.target.value === 'old')}>
                <option value="new">Newest first</option><option value="old">Oldest first</option>
              </select>
            </label>
            <span className="muted small">{rows.length} of {data.length}</span>
          </div>
          <div className="tablewrap">
            <table className="table">
              <thead><tr><th>When</th><th>API</th><th>Specs</th><th>Baseline</th><th>Agent</th><th>Release decision</th></tr></thead>
              <tbody>
                {rows.map((r: AnalysisListItem) => (
                  <tr key={r.id}>
                    <td className="nowrap"><a href={`#/analysis/${r.id}`}>{when(r.created_at)}</a><div className="tiny muted"><code>{r.id}</code></div></td>
                    <td>{r.title ?? 'Untitled'} <span className="muted small">{r.base_version} → {r.candidate_version}</span></td>
                    <td className="small"><code>{r.base_filename}</code> → <code>{r.candidate_filename}</code></td>
                    <td><DecisionWord d={r.decision} /></td>
                    <td>{r.agent_decision ? <DecisionWord d={r.agent_decision} /> : <span className="muted small">not run</span>}</td>
                    <td>
                      {r.review_decision
                        ? <span className={`tag ${r.review_decision === 'APPROVED' ? 'tag--ok' : 'tag--bad'}`}>{r.review_decision === 'APPROVED' ? 'Approved' : 'Rejected'}</span>
                        : <a className="tag tag--warn" href={`#/analysis/${r.id}/decision`}>Awaiting decision</a>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </main>
  )
}

// ---------------------------------------------------------------- audit
export function AuditView() {
  const load = useCallback(() => api.audit(), [])
  const { data, error, run } = useLoad(load)
  const [chain, setChain] = useState<ChainStatus | null>(null)
  const [verifying, setVerifying] = useState(false)
  const [action, setAction] = useState('')
  useEffect(() => { if (data) setChain(data.chain) }, [data])
  const verify = async () => {
    setVerifying(true)
    try { setChain(await api.verifyAudit()); run() } finally { setVerifying(false) }
  }
  const actions = [...new Set((data?.events ?? []).map((e) => e.action))].sort()
  const events = (data?.events ?? []).filter((e) => !action || e.action === action)
  return (
    <main className="page">
      <PageHeader title="Audit log"
        subtitle="Append-only. Each event stores the SHA-256 hash of the previous one, so editing past events breaks the chain. The database also refuses UPDATE and DELETE on this table."
        actions={<>
          <ChainBadge chain={chain} />
          <button type="button" className="btn btn--quiet" onClick={verify} disabled={verifying}>
            {verifying ? <><Spinner size={13} /> Verifying…</> : <><Icon name="check" size={15} /> Verify chain now</>}
          </button>
        </>} />
      {error && <ErrorState title="Could not load the audit log" message={error} onRetry={run} />}
      {!data && !error && <Loading label="Loading audit log…" />}
      {data && data.events.length === 0 && <Empty title="No events yet">Analyses, investigations and release decisions appear here.</Empty>}
      {data && data.events.length > 0 && (
        <>
          <div className="toolbar">
            <label className="select small">Event
              <select value={action} onChange={(e) => setAction(e.target.value)}>
                <option value="">All events</option>
                {actions.map((x) => <option key={x} value={x}>{actionLabel(x)}</option>)}
              </select>
            </label>
            <span className="muted small">{events.length} of {data.events.length} shown (newest first)</span>
          </div>
          <div className="tablewrap">
            <table className="table">
              <thead><tr><th>#</th><th>When</th><th>Event</th><th>Actor</th><th>Details</th><th>Hash</th></tr></thead>
              <tbody>
                {events.map((e: AuditEvent) => (
                  <tr key={e.seq}>
                    <td>{e.seq}</td>
                    <td className="nowrap small">{when(e.ts)}</td>
                    <td className="nowrap">{actionLabel(e.action)}
                      {e.analysis_id && <div className="tiny"><a href={`#/analysis/${e.analysis_id}`}><code>{e.analysis_id}</code></a></div>}
                    </td>
                    <td>{e.actor}</td>
                    <td className="small">{describe(e)}
                      <details><summary className="tiny muted">Raw</summary><pre className="json">{JSON.stringify(e.details, null, 2)}</pre></details>
                    </td>
                    <td className="nowrap"><code className="tiny" title={`prev ${e.prev_hash}\nthis ${e.hash}`}>{e.hash.slice(0, 10)}…</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </main>
  )
}

// ---------------------------------------------------------------- evaluation
const pct = (v: number | string | null | undefined) => (v === null || v === undefined ? 'n/a' : `${Math.round(Number(v) * 1000) / 10}%`)

export function EvaluationView() {
  const [refresh, setRefresh] = useState(false)
  const load = useCallback(() => api.evaluation(refresh), [refresh])
  const { data, error, busy, run } = useLoad<EvaluationReport>(load)
  const m = data?.detection.metrics
  const am = data?.agent.metrics
  return (
    <main className="page">
      <PageHeader title="Evaluation"
        subtitle="Measured on a labelled corpus that ships with the code. Runs offline, in rules mode, on demand."
        actions={<button type="button" className="btn btn--quiet" disabled={busy} onClick={() => { if (refresh) run(); else setRefresh(true) }}>
          {busy ? <><Spinner size={13} /> Running…</> : <><Icon name="refresh" size={15} /> Run again</>}</button>} />
      {error && <ErrorState title="Evaluation failed" message={error} onRetry={run} />}
      {!data && !error && <Loading label="Running the evaluation corpus…" />}
      {data && m && am && (
        <>
          <section className="panel method">
            <h2 className="section__h">Method</h2>
            <ul className="small">
              <li><strong>Change detection:</strong> {m.cases} spec edits, each designed to mean one thing for existing clients (including traps such as a <code>readOnly</code> required field). Labels are the expected rule, severity and release decision, written from the design of each edit, not from engine output.</li>
              <li><strong>Investigation:</strong> {am.cases} cases with real, decoy, noisy or no consumer code. Affected changes were labelled by reading the consumer code.</li>
              <li><strong>Baselines:</strong> simpler gates over the same detected changes, keyword retrieval without verification, and the agent with retries disabled.</li>
            </ul>
          </section>
          <div className="stats">
            <Stat value={pct(m.precision)} label="detection precision" />
            <Stat value={pct(m.recall)} label="detection recall" />
            <Stat value={pct(m.decision_accuracy)} label="baseline decision accuracy" />
            <Stat value={pct(am.accuracy)} label="agent decision accuracy" />
            <Stat value={pct(am.impact_recall)} label="agent impact recall" />
            <Stat value={String(am.false_escalations)} label="false escalations" />
          </div>
          <p className="muted small">{m.cases_fully_correct}/{m.cases} detection cases fully correct ({m.true_positives} TP, {m.false_positives} FP, {m.false_negatives} FN). Ran in {data.duration_ms} ms.</p>

          {data.detection.baselines && (
            <>
              <h2 className="section__h">Release decision: API Sentinel vs simpler gates</h2>
              <div className="tablewrap">
                <table className="table">
                  <thead><tr><th>Gate</th><th>Correct</th><th>Wrong on</th></tr></thead>
                  <tbody>{data.detection.baselines.map((b) => (
                    <tr key={b.system}><td>{b.label}</td><td className="nowrap"><strong>{b.correct}/{b.cases}</strong> ({pct(b.decision_accuracy)})</td>
                      <td className="small">{b.wrong.length ? b.wrong.join('; ') : <span className="muted">none</span>}</td></tr>
                  ))}</tbody>
                </table>
              </div>
            </>
          )}

          {data.agent.comparison && (
            <>
              <h2 className="section__h">Investigation: agent vs simpler approaches</h2>
              <div className="tablewrap">
                <table className="table">
                  <thead><tr><th>System</th><th>Decisions</th><th>False escalations</th><th>Impact precision</th><th>Impact recall</th></tr></thead>
                  <tbody>{data.agent.comparison.map((c) => (
                    <tr key={c.system} className={c.system === 'agent' ? 'is-hl' : ''}><td>{c.label}</td>
                      <td className="nowrap">{c.correct}/{c.cases}</td>
                      <td className={c.false_escalations ? 'error' : ''}>{c.false_escalations}</td>
                      <td className="nowrap">{pct(c.impact_precision)} <span className="muted tiny">({c.impact_tp} TP, {c.impact_fp} FP)</span></td>
                      <td className="nowrap">{pct(c.impact_recall)} <span className="muted tiny">({c.impact_fn} missed)</span></td></tr>
                  ))}</tbody>
                </table>
              </div>
              <div className="tablewrap">
                <table className="table">
                  <thead><tr><th>Case</th><th>Context</th><th>Expected</th><th>Deterministic</th><th>Keyword RAG</th><th>Agent (no retry)</th><th>Agent</th></tr></thead>
                  <tbody>{data.agent.cases.map((c) => (
                    <tr key={c.name}>
                      <td><code>{c.name}</code><div className="tiny muted">{c.why}</div></td>
                      <td>{c.context}</td>
                      <td><DecisionWord d={c.expected} /></td>
                      {(['deterministic', 'keyword_rag', 'agent_no_retry', 'agent'] as const).map((k) => {
                        const s = c.systems?.[k]
                        return <td key={k}>{s ? <><DecisionWord d={s.decision} />{!s.pass && <span className="error"> ✗</span>}</> : '–'}</td>
                      })}
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </>
          )}

          <h2 className="section__h">Change-detection cases</h2>
          <div className="tablewrap">
            <table className="table">
              <thead><tr><th>Case</th><th>OAS</th><th>Expected</th><th>Got</th><th>Changes</th><th>Result</th></tr></thead>
              <tbody>
                {data.detection.cases.map((c) => (
                  <tr key={c.case}>
                    <td><code>{c.case}</code><div className="tiny muted">{c.why}</div>
                      {[...c.missing.map((x) => `missing: ${x}`), ...c.unexpected.map((x) => `unexpected: ${x}`), ...c.severity_mismatches]
                        .map((x) => <div key={x} className="tiny error">{x}</div>)}
                    </td>
                    <td>{c.oas}</td>
                    <td><DecisionWord d={c.expected_decision} /></td>
                    <td><DecisionWord d={c.decision} /></td>
                    <td className="nowrap small">{c.matched}/{c.expected_changes} matched{c.detected !== c.matched ? `, ${c.detected - c.matched} extra` : ''}</td>
                    <td><span className={`tag ${c.pass ? 'tag--ok' : 'tag--bad'}`}>{c.pass ? 'Pass' : 'Fail'}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <section className="callout callout--muted limits">
            <strong>Limits.</strong> The corpus is small and was written alongside the engine. It checks the documented rules and known traps, not how
            often real APIs hit them. It does not prove the agent is better in general. The agent rows run in rules mode, and the LLM
            path is not part of these numbers. Sensitivity was checked by sabotage: deliberately breaking the readOnly handling or the
            operation-call check makes the matching cases fail.
          </section>
        </>
      )}
    </main>
  )
}
