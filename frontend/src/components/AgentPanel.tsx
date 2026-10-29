import { Fragment, lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { FINDING_LABEL, MODE_LABEL, OUTCOME_LABEL, ruleLabel } from '../labels'
import type { AgentRecord, Analysis, Investigation, ServerConfig, TraceEvent } from '../types'
import { DecisionWord, ErrorState, Icon, Spinner, StatusBadge } from '../ui'

const ExecutionGraph = lazy(() => import('./graph/ExecutionGraph').then(m => ({ default: m.ExecutionGraph })))

const NODES = ['baseline', 'plan', 'retrieve', 'assess', 'refine', 'execute', 'verify', 'reflect', 'decide'] as const
const NODE_HELP: Record<string, string> = {
  baseline: 'Load the deterministic result',
  plan: 'Choose which changes need investigation',
  retrieve: 'BM25 search over consumer files and specs',
  assess: 'Judge consumer impact (rules or LLM)',
  refine: 'Generate targeted tests from the evidence',
  execute: 'Run the refined tests',
  verify: 'Check every finding against its source',
  reflect: 'Retry, move on, re-plan or stop',
  decide: 'Revise the recommendation',
}

export function OutcomeTag({ outcome }: { outcome?: string }) {
  if (!outcome) return null
  const o = OUTCOME_LABEL[outcome] ?? { text: outcome, tone: 'muted' as const }
  return <span className={`tag tag--${o.tone}`}>{o.text}</span>
}

function ModeBadge({ mode }: { mode: string }) {
  return <span className={`mode mode--${mode}`}>{MODE_LABEL[mode] ?? mode}</span>
}

// ---------------------------------------------------------------- form + live progress
function InvestigationForm({ a, onStart }: { a: Analysis; onStart: (files: File[], target: string, useSample: boolean) => void }) {
  const [cfg, setCfg] = useState<ServerConfig | null>(null)
  const [files, setFiles] = useState<File[]>([])
  const [target, setTarget] = useState('')
  const [useSample, setUseSample] = useState(true)
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => { api.config().then(setCfg).catch(() => setCfg(null)) }, [])
  return (
    <form className="panel agentform" onSubmit={(e) => { e.preventDefault(); onStart(files, target.trim(), useSample) }}>
      <div className="agentform__grid">
        <div className="field">
          <span className="field__label">Consumer code, logs or docs</span>
          <span className="muted tiny">The knowledge base for retrieval. Text files only, max 25 files, 1 MB each.</span>
          <input ref={input} type="file" multiple hidden onChange={(e) => setFiles([...(e.target.files ?? [])])} />
          <div className="field__inline">
            <button type="button" className="btn btn--quiet" onClick={() => input.current?.click()}><Icon name="file" size={15} /> Add files</button>
            <span className="muted small">
              {files.length ? files.map((f) => f.name).join(', ') : a.sample && useSample ? 'Using the sample’s bundled consumer files' : 'None: impact cannot be assessed'}
            </span>
          </div>
          {a.sample && (
            <label className="check small">
              <input type="checkbox" checked={useSample} onChange={(e) => setUseSample(e.target.checked)} />
              Use the consumer files bundled with this sample when none are added
            </label>
          )}
        </div>
        <label className="field">
          <span className="field__label">Running deployment to replay against (optional)</span>
          <span className="muted tiny">Live HTTP tests. Allowed hosts: {cfg ? cfg.live_allowed_hosts.join(', ') : '…'}</span>
          <input className="text" type="url" placeholder="http://127.0.0.1:8100/v1" value={target} onChange={(e) => setTarget(e.target.value)} />
        </label>
      </div>
      <div className="agentform__foot">
        <p className="muted small">
          Reasoning: {cfg ? (cfg.reasoning
            ? <>local LLM <code>{cfg.reasoning}</code> via Ollama. Every claim must quote the evidence verbatim, or it is discarded. If the model fails, the agent falls back to rules and says so.</>
            : <>rules only (no local model configured). The trace marks every step's mode.</>) : '…'}
        </p>
        <button type="submit" className="btn btn--primary"><Icon name="bolt" size={16} /> Run investigation</button>
      </div>
    </form>
  )
}

function LiveProgress({ events, elapsed }: { events: TraceEvent[]; elapsed: number }) {
  const last = events[events.length - 1]
  const plan = events.find((e) => e.node === 'plan')
  const targets = (plan?.detail.targets as { change_id: string; why: string }[] | undefined) ?? []
  const settled = new Map<string, string>()
  let current: string | null = null
  let retries = 0
  for (const e of events) {
    if (e.node === 'retrieve') current = e.change_id
    if (e.node === 'reflect') {
      if (e.detail.decision === 'retry') retries += 1
      else if (e.change_id) { settled.set(e.change_id, e.summary.split(' settled as ')[1]?.split(' ')[0] ?? ''); current = null }
    }
  }
  return (
    <div className="panel live" role="status" aria-live="polite">
      <p className="live__head"><Spinner /> Investigating… {events.length} steps · {(elapsed / 1000).toFixed(1)} s{retries ? ` · ${retries} retr${retries > 1 ? 'ies' : 'y'}` : ''}</p>
      <ol className="pipeline" aria-label="Workflow stages">
        {NODES.map((n) => {
          const seen = events.some((e) => e.node === n)
          const now = last?.node === n
          return <li key={n} className={`pipeline__n${seen ? ' is-done' : ''}${now ? ' is-now' : ''}`} title={NODE_HELP[n]}>{n}</li>
        })}
      </ol>
      {targets.length > 0 && (
        <ul className="live__targets">
          {targets.map((t) => (
            <li key={t.change_id}>
              <code>{t.change_id}</code>
              {settled.has(t.change_id) ? <OutcomeTag outcome={settled.get(t.change_id)} />
                : current === t.change_id ? <span className="tag tag--info"><Spinner size={10} /> investigating</span>
                : <span className="tag tag--muted">queued</span>}
            </li>
          ))}
        </ul>
      )}
      <ol className="live__log">
        {events.slice(-6).map((e) => (
          <li key={e.seq}><span className="muted">{e.seq}</span> <code>{e.node}</code> <ModeBadge mode={e.mode} /> {e.change_id && <strong>{e.change_id}</strong>} {e.summary}</li>
        ))}
      </ol>
    </div>
  )
}

// ---------------------------------------------------------------- results
function LlmBanner({ inv }: { inv: Investigation }) {
  const l = inv.llm
  if (!l || !l.configured) return <p className="callout callout--muted"><strong>Rules mode.</strong> No LLM is configured, so every reasoning step used deterministic rules (marked “Rules” in the trace).</p>
  if (l.disabled) return (
    <div className="callout callout--warn" role="alert">
      <strong>LLM investigation unavailable ({l.disabled.category}) · local Ollama.</strong> {l.disabled.message} This is a rules-based decision; the investigation finished with rules:
      {' '}{l.failed_calls} failed call{l.failed_calls === 1 ? '' : 's'}, {l.skipped_after_failure} step{l.skipped_after_failure === 1 ? '' : 's'} skipped the model after that.
    </div>
  )
  if (l.failed_calls) return (
    <p className="callout callout--warn"><strong>Partly LLM.</strong> {l.assessments_by_llm} assessment(s) by <code>{l.configured}</code>; {l.failed_calls} fell back to rules ({l.errors.map((e) => e.category).join(', ')}).</p>
  )
  if (l.assessments_by_llm) return (
    <p className="callout callout--info"><strong>LLM reasoning{l.live === false ? ' (test transport, not live)' : ''}.</strong> {l.assessments_by_llm} assessment(s) by <code>{l.configured}</code>{l.tool_calls ? `, ${l.tool_calls} tool call(s) made by the model and executed by API Sentinel` : ''}. Each claim was checked against a verbatim quote from the cited evidence, and against the deterministic rules.</p>
  )
  return <p className="callout callout--muted"><strong>LLM configured but not needed.</strong> No step had consumer context to assess, so rules decided.</p>
}

function Summary({ inv, onPick }: { inv: Investigation; onPick: (id: string) => void }) {
  const d = inv.decision
  const changed = d.decision !== d.baseline_decision
  const reflects = inv.trace.filter((e) => e.node === 'reflect')
  const retries = reflects.filter((e) => e.detail.decision === 'retry').length
  const replans = reflects.filter((e) => e.detail.decision === 'replan').length
  const tools = inv.trace.filter((e) => e.mode === 'tool').length
  const refined = inv.records.flatMap((r) => r.refined_results)
  return (
    <section className={`agentsum agentsum--${d.decision}`}>
      <div className="agentsum__top">
        <div>
          <p className="verdict__label">Investigation result</p>
          <p className="agentsum__word"><DecisionWord d={d.decision} big /></p>
          <p className="small muted">{changed ? <>changed from <DecisionWord d={d.baseline_decision} /></> : 'same as the deterministic baseline'}</p>
        </div>
        <dl className="facts">
          <div><dt>Changes investigated</dt><dd>{inv.records.length}{inv.plan.skipped.length ? ` (+${inv.plan.skipped.length} skipped)` : ''}</dd></div>
          <div><dt>Evidence chunks</dt><dd>{inv.evidence_chunks}</dd></div>
          <div><dt>Tool calls</dt><dd>{tools}</dd></div>
          <div><dt>Retries · re-plans</dt><dd>{retries} · {replans}</dd></div>
          <div><dt>Refined tests</dt><dd>{refined.length}{refined.length ? ` (${refined.filter((r) => r.status === 'failed').length} failed)` : ''}</dd></div>
          <div><dt>Duration</dt><dd>{inv.duration_ms} ms</dd></div>
        </dl>
      </div>
      <h3 className="detail__h">Why</h3>
      <ul className="reasons">
        {d.adjustments.map((x, i) => (
          <li key={i}>
            {x.text}
            {x.change_ids.length > 0 && <span className="reasons__ids">{x.change_ids.map((id) => <button key={id} type="button" className="chip" onClick={() => onPick(id)}>{id}</button>)}</span>}
            {x.evidence_ids.length > 0 && <span className="muted small"> Evidence {x.evidence_ids.join(', ')}.</span>}
            {x.test_ids.length > 0 && <span className="muted small"> Tests {x.test_ids.join(', ')}.</span>}
          </li>
        ))}
      </ul>
      <p className="muted tiny">
        Consumer files: {inv.context_files.length ? inv.context_files.join(', ') : 'none'}{inv.target_url ? ` · live target ${inv.target_url}` : ''} · reasoning: {inv.reasoning} · policy: {d.policy}
      </p>
    </section>
  )
}

function ChangeRows({ a, inv }: { a: Analysis; inv: Investigation }) {
  const [open, setOpen] = useState<string | null>(null)
  const byId = useMemo(() => new Map(a.changes.map((c) => [c.id, c])), [a.changes])
  return (
    <div className="tablewrap">
      <table className="table table--rows">
        <thead><tr><th>Change</th><th>Retrieval</th><th>Findings</th><th>Refined tests</th><th>Outcome</th></tr></thead>
        <tbody>
          {inv.records.map((r) => {
            const c = byId.get(r.change_id)
            const ver = r.findings.filter((f) => f.verified).length
            const atts = r.attempts ?? []
            return (
              <Fragment key={r.change_id}>
                <tr className={`clickrow${open === r.change_id ? ' is-open' : ''}`} onClick={() => setOpen(open === r.change_id ? null : r.change_id)}>
                  <td><button type="button" className="linkbtn" aria-expanded={open === r.change_id}>{r.change_id}</button> {c && ruleLabel(c.rule)}<div className="tiny muted">{c?.location}</div></td>
                  <td className="small">{atts.length ? atts.map((x) => x.strategy).join(' → ') : '1 attempt'}<div className="tiny muted">{r.evidence.filter((e) => e.kind === 'context').length} consumer chunk(s)</div></td>
                  <td className="small">{ver} verified{r.findings.length - ver ? <span className="error"> · {r.findings.length - ver} discarded</span> : ''} <ModeBadge mode={r.mode} /></td>
                  <td className="small">{r.refined_results.length ? r.refined_results.map((x) => <StatusBadge key={x.test_id} status={x.status} />) : <span className="muted">none</span>}</td>
                  <td><OutcomeTag outcome={r.outcome} /></td>
                </tr>
                {open === r.change_id && <tr className="expand"><td colSpan={5}><AgentEvidence rec={r} /></td></tr>}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function EventDetail({ e }: { e: TraceEvent }) {
  const d = e.detail as Record<string, unknown>
  const hits = d.hits as { id: string; citation: string; score: number }[] | undefined
  const results = d.results as { test_id: string; status: string; summary: string }[] | undefined
  const verification = d.verification as { chunk_id: string; finding: string; verified: boolean; verification: string }[] | undefined
  const findings = d.findings as { chunk_id: string; finding: string; citation?: string; detail?: string }[] | undefined
  const targets = d.targets as { change_id: string; why: string }[] | undefined
  return (
    <div className="trace__detail">
      {Array.isArray(d.query) && <p className="small"><strong>Query</strong> ({String(d.strategy)}, top {String(d.k)}): <code>{(d.query as string[]).join(' ')}</code></p>}
      {hits && <ul className="small">{hits.map((h) => <li key={h.id}><code>{h.id}</code> {h.citation} <span className="muted">score {h.score}</span></li>)}</ul>}
      {typeof d.rationale === 'string' && <p className="small"><strong>Rationale</strong> {d.rationale}</p>}
      {d.llm !== null && typeof d.llm === 'object' && <p className="small"><strong>Model</strong> <code>{String((d.llm as { model: string }).model)}</code>{(d.llm as { live?: boolean }).live ? ' · live request' : ' · NOT a live request (test transport)'}</p>}
      {Array.isArray(d.tool_calls) && d.tool_calls.length > 0 && (
        <ul className="small">{(d.tool_calls as { round: number; tool: string; arguments: unknown; result_ids: string[]; error: string | null }[]).map((t, i) => (
          <li key={i}>model tool call {t.round}: <code>{t.tool}</code> <code>{typeof t.arguments === 'string' ? t.arguments : JSON.stringify(t.arguments)}</code>{t.error ? <span className="error"> rejected: {t.error}</span> : <> → executed, returned {t.result_ids.join(', ') || 'nothing'}</>}</li>
        ))}</ul>
      )}
      {findings && findings.length > 0 && <ul className="small">{findings.map((f, i) => <li key={i}><code>{f.chunk_id}</code> {FINDING_LABEL[f.finding] ?? f.finding} {f.citation && <span className="muted">({f.citation})</span>}</li>)}</ul>}
      {results && <ul className="small">{results.map((r) => <li key={r.test_id}><code>{r.test_id}</code> <StatusBadge status={r.status} /> {r.summary}</li>)}</ul>}
      {verification && <ul className="small">{verification.map((v, i) => <li key={i}><code>{v.chunk_id}</code> {FINDING_LABEL[v.finding] ?? v.finding}: {v.verified ? <span className="ok">verified</span> : <span className="error">discarded</span>} <span className="muted">({v.verification})</span></li>)}</ul>}
      {targets && <ul className="small">{targets.map((t) => <li key={t.change_id}><code>{t.change_id}</code> {t.why}</li>)}</ul>}
      {typeof d.decision === 'string' && e.node === 'reflect' && <p className="small"><strong>Decision</strong> {d.decision}{typeof d.reason === 'string' ? `: ${d.reason}` : ''}{Array.isArray(d.moved_forward) && d.moved_forward.length ? ` · moved forward ${(d.moved_forward as string[]).join(', ')}` : ''}</p>}
      <details><summary className="tiny muted">Raw event</summary><pre className="json">{JSON.stringify(e.detail, null, 2)}</pre></details>
    </div>
  )
}

const TRACE_FILTERS = {
  all: { label: 'All steps', fn: () => true },
  tools: { label: 'Tool calls', fn: (e: TraceEvent) => e.mode === 'tool' },
  reasoning: { label: 'Reasoning', fn: (e: TraceEvent) => e.node === 'assess' || e.node === 'verify' || e.node === 'refine' },
  control: { label: 'Planning & control', fn: (e: TraceEvent) => ['plan', 'reflect', 'decide', 'baseline'].includes(e.node) },
} as const

export function Trace({ trace, onPick }: { trace: TraceEvent[]; onPick: (id: string) => void }) {
  const [open, setOpen] = useState<number | null>(null)
  const [f, setF] = useState<keyof typeof TRACE_FILTERS>('all')
  const [cid, setCid] = useState('')
  const ids = [...new Set(trace.map((e) => e.change_id).filter(Boolean))] as string[]
  const shown = trace.filter((e) => TRACE_FILTERS[f].fn(e) && (!cid || e.change_id === cid))
  return (
    <section className="tracebox" aria-label="Execution trace">
      <div className="toolbar">
        <div className="filters" role="group" aria-label="Filter trace">
          {(Object.keys(TRACE_FILTERS) as (keyof typeof TRACE_FILTERS)[]).map((k) => (
            <button key={k} type="button" aria-pressed={f === k} className={`filter${f === k ? ' filter--on' : ''}`} onClick={() => setF(k)}>{TRACE_FILTERS[k].label}</button>
          ))}
        </div>
        <label className="select small">Change
          <select value={cid} onChange={(e) => setCid(e.target.value)}>
            <option value="">All</option>
            {ids.map((i) => <option key={i} value={i}>{i}</option>)}
          </select>
        </label>
      </div>
      <ol className="trace">
        {shown.map((e) => (
          <li key={e.seq} className={`trace__ev trace__ev--${e.node}`} id={`trace-ev-${e.seq}`}>
            <button type="button" className="trace__row" onClick={() => setOpen(open === e.seq ? null : e.seq)} aria-expanded={open === e.seq}>
              <span className="trace__seq">{e.seq}</span>
              <code className="trace__node">{e.node}</code>
              <ModeBadge mode={e.mode} />
              <span className="trace__sum">{e.change_id && <span className="trace__cid">{e.change_id}</span>} {e.summary}</span>
              <span className="muted tiny trace__ms">{e.duration_ms} ms</span>
            </button>
            {open === e.seq && (
              <>
                {e.change_id && <button type="button" className="btn btn--tiny trace__show" onClick={() => onPick(e.change_id!)}>Show {e.change_id} in Changes</button>}
                <EventDetail e={e} />
              </>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}

export function InvestigationPanel({ a, onChange, onPick }: { a: Analysis; onChange: (a: Analysis) => void; onPick: (id: string) => void }) {
  const inv = a.investigation ?? null
  const [mode, setMode] = useState<'view' | 'form' | 'running'>(inv ? 'view' : 'form')
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [last, setLast] = useState<{ files: File[]; target: string; useSample: boolean } | null>(null)
  const [showTrace, setShowTrace] = useState(false)
  const [traceView, setTraceView] = useState<'graph' | 'list'>('graph')
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const start = async (files: File[], target: string, useSample: boolean) => {
    setLast({ files, target, useSample }); setError(null); setEvents([]); setElapsed(0); setMode('running')
    try {
      const { job_id } = await api.startInvestigation(a.id, files, target, useSample)
      let since = 0
      for (;;) {
        if (!alive.current) return
        const j = await api.job(job_id, since)
        since = j.event_count
        if (j.events.length) setEvents((ev) => [...ev, ...j.events])
        setElapsed(j.elapsed_ms)
        if (j.status === 'done') { onChange(await api.get(a.id)); setMode('view'); setShowTrace(false); return }
        if (j.status === 'failed') throw new Error(j.error ?? 'Investigation failed')
        await new Promise((r) => setTimeout(r, 350))
      }
    } catch (e) {
      setError((e as Error).message); setMode('form')
    }
  }

  return (
    <div className="invest">
      <p className="muted invest__intro">
        Investigate changes the contract tests could not settle. The agent retrieves consumer evidence, assesses impact,
        runs refined tests, and verifies its findings — retrying or re-planning as needed. Every step below really ran.
      </p>
      {error && <ErrorState title="Investigation failed" message={error} onRetry={last ? () => start(last.files, last.target, last.useSample) : undefined} />}
      {mode === 'form' && <InvestigationForm a={a} onStart={start} />}
      {mode === 'running' && <LiveProgress events={events} elapsed={elapsed} />}
      {mode === 'view' && inv && (
        <>
          <div className="invest__bar">
            <span className="muted small">Ran {new Date(inv.created_at).toLocaleString()} · {inv.trace.length} steps</span>
            <button type="button" className="btn btn--quiet" onClick={() => setMode('form')}><Icon name="refresh" size={15} /> Run again</button>
          </div>
          <LlmBanner inv={inv} />
          <Summary inv={inv} onPick={onPick} />
          <h3 className="section__h">Per-change investigation</h3>
          {inv.records.length ? <ChangeRows a={a} inv={inv} /> : <p className="muted">Nothing needed investigation: every change was settled by the contract tests.</p>}
          <div className="tracehead">
            <h3 className="section__h">Execution trace</h3>
            <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
              <div className="gtoggle">
                <button type="button" className={`gtoggle__btn${traceView === 'graph' ? ' gtoggle__btn--on' : ''}`}
                  onClick={() => { setTraceView('graph'); setShowTrace(true) }}>Graph</button>
                <button type="button" className={`gtoggle__btn${traceView === 'list' ? ' gtoggle__btn--on' : ''}`}
                  onClick={() => { setTraceView('list'); setShowTrace(true) }}>List</button>
              </div>
              <button type="button" className="btn btn--quiet" onClick={() => setShowTrace(!showTrace)} aria-expanded={showTrace}>
                {showTrace ? 'Hide' : `Show ${inv.trace.length} steps`}
              </button>
            </div>
          </div>
          <ol className="pipeline pipeline--static" aria-label="Graph nodes">{NODES.map((n) => <li key={n} className="pipeline__n is-done" title={NODE_HELP[n]}>{n}</li>)}</ol>
          {showTrace && traceView === 'list' && <Trace trace={inv.trace} onPick={onPick} />}
          {showTrace && traceView === 'graph' && (
            <Suspense fallback={<div className="state state--loading"><Spinner /> Loading graph…</div>}>
              <ExecutionGraph
                trace={inv.trace}
                onShowInTrace={(seq) => { setTraceView('list'); setShowTrace(true); setTimeout(() => document.getElementById(`trace-ev-${seq}`)?.scrollIntoView({ block: 'nearest' }), 100) }}
              />
            </Suspense>
          )}
        </>
      )}
    </div>
  )
}

export function AgentEvidence({ rec }: { rec: AgentRecord }) {
  const results = new Map(rec.refined_results.map((r) => [r.test_id, r]))
  const used = new Set(rec.findings.map((f) => f.chunk_id))
  return (
    <section className="agentev">
      <header className="agentev__head">
        <h4>Agent investigation of {rec.change_id}</h4>
        <OutcomeTag outcome={rec.outcome} />
        <ModeBadge mode={rec.mode} />
      </header>
      <p className="small">{rec.rationale}</p>
      {rec.note && <p className="callout callout--warn small">{rec.note}</p>}
      {rec.attempts && rec.attempts.length > 1 && (
        <p className="small muted">
          Retrieval attempts: {rec.attempts.map((x) => `${x.strategy} (${x.hits.length} hits, ${x.verified} verified)`).join(' → ')}
        </p>
      )}
      {rec.tool_calls && rec.tool_calls.length > 0 && (
        <div className="toolcalls">
          <p className="small"><strong>Tool calls made by the model ({rec.tool_calls.length})</strong>: each was executed by API Sentinel against the evidence index.</p>
          <ul className="small">
            {rec.tool_calls.map((t, i) => (
              <li key={i}>
                round {t.round}: <code>{t.tool}({typeof t.arguments === 'string' ? t.arguments : JSON.stringify(t.arguments)})</code>
                {t.error ? <span className="error"> · rejected: {t.error}</span> : <> → {t.result_ids.length ? t.result_ids.map((id) => <code key={id} className="chip">{id}</code>) : 'no results'}</>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {rec.cross_check && rec.cross_check.length > 0 && rec.outcome === 'contradictory_evidence' && (
        <p className="callout callout--warn small">The model judged this change safe, but deterministic rules found consumer usage in {rec.cross_check.map((x) => x.citation ?? x.chunk_id).join(', ')}. Left for a human.</p>
      )}
      {rec.findings.length > 0 && (
        <ul className="findings">
          {rec.findings.map((f, i) => (
            <li key={i} className={f.verified ? '' : 'findings--rejected'}>
              <p className="small">
                <code>{f.citation ?? f.chunk_id}</code> {FINDING_LABEL[f.finding] ?? f.finding}
                {f.verified ? <span className="ok"> · verified</span> : <strong className="error"> · discarded: {f.verification}</strong>}
              </p>
              {(f.lines ?? []).map((l) => <pre key={l.line} className="cite"><span className="cite__n">{l.line}</span>{l.text}</pre>)}
              {f.quote && <pre className="cite">{f.quote}</pre>}
              {f.operation_call && !(f.lines ?? []).some((l) => l.line === f.operation_call!.line) && (
                <p className="muted tiny">Calls the operation at line {f.operation_call.line}: <code>{f.operation_call.text}</code></p>
              )}
            </li>
          ))}
        </ul>
      )}
      {rec.refined_tests.map((t) => {
        const r = results.get(t.id)
        return (
          <article key={t.id} className="test">
            <header><h4>{t.title}</h4>{r && <StatusBadge status={r.status} />}</header>
            <p className="muted tiny"><code>{t.id}</code> · {t.kind.replace(/_/g, ' ')} · refined by the agent{t.based_on ? ` from ${t.based_on}` : ''}</p>
            <p className="small">{t.rationale}</p>
            {t.derived_from && <ul className="notes">{t.derived_from.map((d, i) => <li key={i}>{d.constraint} (evidence {d.evidence.join(', ')})</li>)}</ul>}
            {r && (
              <div className="test__result">
                <p className="small"><strong>{r.summary}</strong></p>
                {r.errors.length > 0 && <ul className="errors">{r.errors.map((e, i) => <li key={i}><code>{e.path}</code> {e.message}</li>)}</ul>}
                {r.request && <details><summary className="small">Request sent</summary><pre className="json">{JSON.stringify(r.request, null, 2)}</pre></details>}
                {r.response && <details><summary className="small">Reply</summary><pre className="json">{JSON.stringify(r.response, null, 2)}</pre></details>}
              </div>
            )}
          </article>
        )
      })}
      {rec.evidence.length > 0 && (
        <details className="retrieved">
          <summary className="small">Retrieved evidence ({rec.evidence.length} chunks, BM25)</summary>
          <ul>
            {rec.evidence.map((e) => (
              <li key={e.id}>
                <p className="tiny"><code>{e.id}</code> {e.citation} <span className="muted">· {e.kind} · score {e.score}{used.has(e.id) ? ' · cited' : ''}</span></p>
                <pre className="json">{e.text}</pre>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

