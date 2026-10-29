import { useMemo, useState } from 'react'
import { CATEGORY_ORDER, DECISION_COPY, SEVERITY_LABEL, VERDICT_LABEL, categoryOf, ruleLabel, ruleWhy } from '../labels'
import type { AgentRecord, Analysis, Change, Severity, TestCase, TestResult } from '../types'
import { DecisionWord, Empty, Icon, PageHeader, Stat, StatusBadge, Tabs } from '../ui'
import { AgentEvidence, InvestigationPanel, OutcomeTag } from './AgentPanel'
import { AuditTrail, ReviewPanel } from './ReviewPanel'

export type ResultTab = 'changes' | 'investigation' | 'decision' | 'audit'
type GroupBy = 'operation' | 'category' | 'severity'

const FILTERS: { key: 'ALL' | Severity; label: string }[] = [
  { key: 'ALL', label: 'All' },
  { key: 'BREAKING', label: 'Breaking' },
  { key: 'POTENTIALLY_BREAKING', label: 'Potentially breaking' },
  { key: 'NON_BREAKING', label: 'Safe' },
]

export function SevMark({ severity }: { severity: Severity }) {
  return <span className={`sev sev--${severity}`} role="img" aria-label={SEVERITY_LABEL[severity]} title={SEVERITY_LABEL[severity]} />
}

function Json({ value }: { value: unknown }) {
  if (value === undefined || value === null) return <span className="muted small">(not present)</span>
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>
}

const operationOf = (c: Change) => (c.method && c.path ? `${c.method.toUpperCase()} ${c.path}` : 'Whole document')
const detailOf = (c: Change) => {
  const parts = c.location.split(' › ')
  return c.method && c.path ? parts.slice(1).join(' › ') : c.location
}

type Evidence = { text: string; tone: 'bad' | 'warn' | 'ok' | 'muted'; short: string }

function evidenceOf(c: Change, ev: string | undefined, inconclusive: boolean): Evidence {
  if (ev === 'error') return { short: 'Test error', tone: 'warn', text: 'The contract test could not run, so this change has no test verdict.' }
  if (!ev || ev === 'untested') return { short: 'Informational', tone: 'muted', text: 'Informational change: no compatibility test is needed.' }
  if (c.severity === 'BREAKING') {
    if (ev === 'incompatibility_observed') return { short: 'Confirmed', tone: 'bad', text: 'Confirmed break: a contract test reproduced what an existing client would experience.' }
    return { short: 'Not confirmed', tone: 'warn', text: inconclusive
      ? 'Predicted break, not confirmed: a schema check cannot see this kind of change (for example removing an optional field), so consumer evidence or a reviewer must decide.'
      : 'Predicted break, but the contract test passed. A reviewer should look at it.' }
  }
  if (c.severity === 'POTENTIALLY_BREAKING') {
    if (ev === 'incompatibility_observed') return { short: 'Risk observed', tone: 'warn', text: 'Potential risk with test evidence: old clients will see something their contract did not allow. Whether that breaks them depends on how they handle it.' }
    return { short: 'Potential', tone: 'warn', text: 'Potential risk: the test did not show an incompatibility, but behaviour may still change for some clients.' }
  }
  if (ev === 'incompatibility_observed') return { short: 'Unexpected failure', tone: 'bad', text: 'Classified as safe, but a compatibility test failed. Inspect it.' }
  return { short: 'Compatible', tone: 'ok', text: 'Compatible: the test confirms existing clients are unaffected.' }
}

function TestBlock({ t, r }: { t: TestCase; r?: TestResult }) {
  return (
    <article className="test">
      <header>
        <h4>{t.title}</h4>
        {r && <StatusBadge status={r.status} />}
      </header>
      <p className="muted small">
        <code>{t.id}</code> · {t.kind.replace(/_/g, ' ')} · expected: {t.predicted === 'incompatible' ? 'old clients break' : t.predicted === 'compatible' ? 'old clients unaffected' : 'unknown'}
        {r && <> · {VERDICT_LABEL[r.verdict] ?? r.verdict}{r.duration_ms ? ` · ${r.duration_ms} ms` : ''}</>}
      </p>
      <p className="small">{t.rationale}</p>
      {r && (
        <div className="test__result">
          <p><strong>{r.summary}</strong></p>
          {r.errors.length > 0 && (
            <ul className="errors">{r.errors.map((e, i) => <li key={i}><code>{e.path}</code> {e.message}</li>)}</ul>
          )}
          {r.collateral && r.collateral.length > 0 && (
            <details><summary className="small">Also observed, caused by other changes ({r.collateral.length})</summary>
              <ul className="errors errors--muted">{r.collateral.map((e, i) => <li key={i}><code>{e.path}</code> {e.message}</li>)}</ul>
            </details>
          )}
        </div>
      )}
      <details>
        <summary className="small">Generated input</summary>
        <Json value={t.payload} />
      </details>
      {t.notes.length > 0 && <ul className="notes">{t.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>}
    </article>
  )
}

function ChangeDetail({ a, change, rec, ev }: { a: Analysis; change: Change | null; rec?: AgentRecord; ev?: Evidence }) {
  if (!change || !ev) return <aside className="detail detail--empty"><p>Select a change to see why it matters and the test that checked it.</p></aside>
  const tests = a.tests.filter((t) => t.change_ids.includes(change.id))
  const results = new Map(a.results.map((r) => [r.test_id, r]))
  return (
    <aside className="detail" aria-live="polite">
      <header className="detail__head">
        <SevMark severity={change.severity} />
        <div>
          <h3>{ruleLabel(change.rule)}</h3>
          <p className="muted small">{SEVERITY_LABEL[change.severity]} · {change.direction} side · <code>{change.rule}</code> · <code>{change.id}</code></p>
        </div>
      </header>
      <p className="detail__loc"><code>{change.location}</code></p>
      <div className={`callout callout--${ev.tone}`}><strong>{ev.short}.</strong> {ev.text}</div>
      <h4 className="detail__h">What changed</h4>
      <p>{change.message}</p>
      {change.component && <p className="muted small">Defined in schema component <code>{change.component}</code>, so every operation using it is affected.</p>}
      {(change.before !== null || change.after !== null) && (
        <div className="beforeafter">
          <div><h5>Baseline (live)</h5><Json value={change.before} /></div>
          <div><h5>Candidate</h5><Json value={change.after} /></div>
        </div>
      )}
      <h4 className="detail__h">Why it matters for existing clients</h4>
      <p>{ruleWhy(change.rule, change.severity)}</p>
      {rec && <AgentEvidence rec={rec} />}
      <h4 className="detail__h">Contract tests ({tests.length})</h4>
      {tests.length === 0 ? <p className="muted small">Informational change: no compatibility test was generated.</p>
        : tests.map((t) => <TestBlock key={t.id} t={t} r={results.get(t.id)} />)}
    </aside>
  )
}

function ChangesTab({ a, records, selected, setSelected }: {
  a: Analysis; records: Map<string, AgentRecord>; selected: string | null; setSelected: (id: string) => void
}) {
  const [filter, setFilter] = useState<'ALL' | Severity>('ALL')
  const [groupBy, setGroupBy] = useState<GroupBy>('operation')
  const [q, setQ] = useState('')
  const status = a.recommendation.change_status
  const inconclusive = useMemo(() => {
    const verdicts = new Map(a.results.map((r) => [r.test_id, r.verdict]))
    const out = new Set<string>()
    for (const t of a.tests) if (verdicts.get(t.id) === 'inconclusive') t.change_ids.forEach((id) => out.add(id))
    return out
  }, [a.tests, a.results])
  const evidence = useMemo(() => new Map(a.changes.map((c) => [c.id, evidenceOf(c, status[c.id]?.evidence, inconclusive.has(c.id))])),
    [a.changes, status, inconclusive])

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const m = new Map<string, Change[]>()
    for (const c of a.changes) {
      if (filter !== 'ALL' && c.severity !== filter) continue
      if (needle && !`${c.id} ${c.location} ${c.rule} ${ruleLabel(c.rule)} ${c.message}`.toLowerCase().includes(needle)) continue
      const k = groupBy === 'operation' ? operationOf(c) : groupBy === 'category' ? categoryOf(c) : SEVERITY_LABEL[c.severity]
      m.set(k, [...(m.get(k) ?? []), c])
    }
    const entries = [...m.entries()]
    if (groupBy === 'category') entries.sort((x, y) => CATEGORY_ORDER.indexOf(x[0] as never) - CATEGORY_ORDER.indexOf(y[0] as never))
    if (groupBy === 'severity') {
      const order = [SEVERITY_LABEL.BREAKING, SEVERITY_LABEL.POTENTIALLY_BREAKING, SEVERITY_LABEL.NON_BREAKING]
      entries.sort((x, y) => order.indexOf(x[0]) - order.indexOf(y[0]))
    }
    return entries
  }, [a.changes, filter, groupBy, q])

  const count = (k: 'ALL' | Severity) => (k === 'ALL' ? a.changes.length : a.changes.filter((c) => c.severity === k).length)
  const current = a.changes.find((c) => c.id === selected) ?? null
  const shown = groups.reduce((n, [, items]) => n + items.length, 0)

  if (a.changes.length === 0) return <Empty title="No structural differences">The two specs describe the same contract. Nothing to test.</Empty>
  return (
    <div className="workspace">
      <section className="ledger" aria-label="Changes">
        <div className="toolbar">
          <label className="search">
            <Icon name="search" size={16} />
            <input type="search" placeholder="Search path, field, rule…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search changes" />
          </label>
          <label className="select small">
            Group by
            <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as GroupBy)}>
              <option value="operation">Endpoint</option>
              <option value="category">Category</option>
              <option value="severity">Severity</option>
            </select>
          </label>
        </div>
        <div className="filters" role="group" aria-label="Filter by severity">
          {FILTERS.map((f) => (
            <button key={f.key} aria-pressed={filter === f.key} type="button"
              className={`filter${filter === f.key ? ' filter--on' : ''}`} onClick={() => setFilter(f.key)}>
              {f.key !== 'ALL' && <SevMark severity={f.key} />}{f.label} <span className="filter__n">{count(f.key)}</span>
            </button>
          ))}
        </div>
        {shown === 0 && <p className="muted small">No changes match.</p>}
        {groups.map(([g, items]) => (
          <div key={g} className="group">
            <h3 className="group__h">{groupBy === 'operation' ? <code>{g}</code> : g} <span className="muted">{items.length}</span></h3>
            <ul>
              {items.map((c) => {
                const ev = evidence.get(c.id)!
                return (
                  <li key={c.id} id={`row-${c.id}`}>
                    <button type="button" className={`row${selected === c.id ? ' row--on' : ''}`} onClick={() => setSelected(c.id)}
                      aria-current={selected === c.id}>
                      <SevMark severity={c.severity} />
                      <span className="row__main">
                        <span className="row__rule">{ruleLabel(c.rule)} <span className="row__id">{c.id}</span></span>
                        <span className="row__where">{groupBy === 'operation' ? detailOf(c) || '(operation)' : c.location}</span>
                      </span>
                      <span className="row__side">
                        <span className={`tag tag--${ev.tone}`}>{ev.short}</span>
                        <OutcomeTag outcome={records.get(c.id)?.outcome} />
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        ))}
      </section>
      <ChangeDetail a={a} change={current} rec={current ? records.get(current.id) : undefined}
        ev={current ? evidence.get(current.id) : undefined} />
    </div>
  )
}

function NextSteps({ a, onTab }: { a: Analysis; onTab: (t: ResultTab) => void }) {
  const inv = a.investigation
  const review = a.reviews[0]
  const needsInvestigation = a.recommendation.decision !== 'PROCEED'
  const investigateNext = !inv && needsInvestigation
  const steps = [
    { tab: 'changes' as const, n: 1, title: 'Inspect changes', state: 'done', text: `${a.changes.length} found, ${a.tests.length} tests run` },
    { tab: 'investigation' as const, n: 2, title: 'Investigate consumers',
      state: inv ? 'done' : investigateNext ? 'next' : 'optional',
      text: inv ? `Agent: ${DECISION_COPY[inv.decision.decision].word}` : needsInvestigation ? 'Recommended: check who is affected' : 'Optional: nothing needs judgement' },
    { tab: 'decision' as const, n: 3, title: 'Record release decision', state: review ? 'done' : investigateNext ? 'pending' : 'next',
      text: review ? `${review.decision === 'APPROVED' ? 'Approved' : 'Rejected'} by ${review.reviewer}` : 'Awaiting a reviewer' },
  ]
  return (
    <ol className="steps" aria-label="Workflow">
      {steps.map((s) => (
        <li key={s.n}>
          <button type="button" className={`steps__item steps__item--${s.state}`} onClick={() => onTab(s.tab)}>
            <span className="steps__n">{s.state === 'done' ? <Icon name="check" size={14} /> : s.n}</span>
            <span><span className="steps__t">{s.title}</span><span className="steps__x">{s.text}</span></span>
          </button>
        </li>
      ))}
    </ol>
  )
}

function Verdict({ a, onPick }: { a: Analysis; onPick: (id: string) => void }) {
  const inv = a.investigation
  const rec = a.recommendation
  const eff = inv ? inv.decision.decision : rec.decision
  const escalated = inv && inv.decision.decision === 'BLOCK' && rec.decision !== 'BLOCK'
  const copy = escalated
    ? { word: DECISION_COPY.BLOCK.word, line: 'Consumer evidence shows existing clients will break, and refined tests confirm it.' }
    : DECISION_COPY[eff]
  const review = a.reviews[0]
  const reasons = inv ? inv.decision.adjustments.map((x) => ({ code: x.code, text: x.text, ids: x.change_ids }))
    : rec.reasons.map((r) => ({ code: r.code, text: r.text, ids: r.change_ids }))
  return (
    <section className={`verdict verdict--${eff}`} aria-labelledby="verdict-word">
      <div className="verdict__word">
        <p className="verdict__label">{inv ? 'Agent recommendation' : 'Recommendation'}</p>
        <h2 id="verdict-word">{copy.word}</h2>
        <p className="verdict__trail small">
          Baseline <DecisionWord d={rec.decision} />{inv && <> → agent <DecisionWord d={inv.decision.decision} /></>}
        </p>
      </div>
      <div className="verdict__body">
        <p className="verdict__line">{copy.line}</p>
        {reasons.length > 0 && (
          <ul className="reasons">
            {reasons.slice(0, 5).map((r, i) => (
              <li key={`${r.code}-${i}`}>
                {r.text}
                {r.ids.length > 0 && (
                  <span className="reasons__ids">
                    {r.ids.slice(0, 8).map((id) => <button key={id} type="button" className="chip" onClick={() => onPick(id)}>{id}</button>)}
                    {r.ids.length > 8 && <span className="muted small">+{r.ids.length - 8}</span>}
                  </span>
                )}
              </li>
            ))}
            {reasons.length > 5 && <li className="muted small">and {reasons.length - 5} more</li>}
          </ul>
        )}
        <p className="verdict__human small muted">
          {review
            ? <><strong>Release {review.decision === 'APPROVED' ? 'approved' : 'rejected'}</strong> by {review.reviewer}{review.overrides_recommendation ? ' (override)' : ''}.</>
            : <>A person approves or rejects the release in <strong>Release decision</strong>.</>}
        </p>
      </div>
    </section>
  )
}

export function ResultsView({ a, tab, onTab, onChange }: {
  a: Analysis; tab: ResultTab; onTab: (t: ResultTab) => void; onChange: (a: Analysis) => void
}) {
  const inv = a.investigation ?? null
  const records = useMemo(() => new Map((inv?.records ?? []).map((r) => [r.change_id, r])), [inv])
  const [selected, setSelected] = useState<string | null>(a.changes[0]?.id ?? null)
  const n = a.recommendation.counts
  const status = a.recommendation.change_status
  const confirmed = a.changes.filter((c) => c.severity === 'BREAKING' && status[c.id]?.evidence === 'incompatibility_observed').length
  const pick = (id: string) => {
    setSelected(id)
    onTab('changes')
    requestAnimationFrame(() => document.getElementById(`row-${id}`)?.scrollIntoView({ block: 'nearest' }))
  }

  return (
    <main className="page page--wide">
      <PageHeader
        eyebrow={<>Analysis <code>{a.id}</code> · {new Date(a.created_at).toLocaleString()}</>}
        title={<>{a.base.title ?? 'Untitled API'} <span className="muted">{a.base.api_version} → {a.candidate.api_version}</span></>}
        subtitle={<>Baseline <code>{a.base.filename}</code> (OpenAPI {a.base.openapi_version}) compared with candidate <code>{a.candidate.filename}</code> (OpenAPI {a.candidate.openapi_version}).</>}
        actions={<a className="btn btn--quiet" href="#"><Icon name="compare" size={16} /> New comparison</a>} />

      <Verdict a={a} onPick={pick} />

      <div className="stats">
        <Stat value={n.changes} label="changes" />
        <Stat value={<>{n.breaking}<small> ({confirmed} confirmed)</small></>} label="breaking" tone={n.breaking ? 'bad' : undefined} />
        <Stat value={n.potentially_breaking} label="potentially breaking" tone={n.potentially_breaking ? 'warn' : undefined} />
        <Stat value={n.non_breaking} label="safe" tone="ok" />
        <Stat value={<>{n.tests_failed}<small> / {n.tests}</small></>} label="tests failed" tone={n.tests_failed ? 'bad' : undefined} />
        <Stat value={n.tests_error} label="test errors" tone={n.tests_error ? 'warn' : undefined} />
      </div>

      <NextSteps a={a} onTab={onTab} />

      <Tabs label="Analysis sections" active={tab} onChange={onTab} tabs={[
        { id: 'changes', label: 'Changes', badge: a.changes.length },
        { id: 'investigation', label: 'Investigation', badge: inv ? DECISION_COPY[inv.decision.decision].word : undefined },
        { id: 'decision', label: 'Release decision', badge: a.reviews[0] ? (a.reviews[0].decision === 'APPROVED' ? 'Approved' : 'Rejected') : undefined },
        { id: 'audit', label: 'Audit trail' },
      ]} />
      <div className="tabpanel" role="tabpanel" aria-labelledby={`tab-${tab}`}>
        {tab === 'changes' && <ChangesTab a={a} records={records} selected={selected} setSelected={setSelected} />}
        {tab === 'investigation' && <InvestigationPanel a={a} onChange={onChange} onPick={pick} />}
        {tab === 'decision' && <ReviewPanel a={a} onChange={onChange} onPick={pick} />}
        {tab === 'audit' && <AuditTrail analysisId={a.id} refreshKey={`${inv?.id ?? ''}-${a.reviews.length}`} />}
      </div>
    </main>
  )
}
