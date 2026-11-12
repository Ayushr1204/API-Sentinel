import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { Analysis, Decision, Sample, SpecSummary } from '../types'
import { DecisionWord, ErrorState, Icon, Loading, PageHeader, Spinner } from '../ui'

interface SlotState {
  file: File | null
  summary: SpecSummary | null
  checking: boolean
  error: string | null
}

const empty: SlotState = { file: null, summary: null, checking: false, error: null }
const MAX_BYTES = 2 * 1024 * 1024
const EXT = /\.(ya?ml|json)$/i

function precheck(f: File): string | null {
  if (!EXT.test(f.name)) return `${f.name} is not a .yaml, .yml or .json file.`
  if (f.size === 0) return `${f.name} is empty.`
  if (f.size > MAX_BYTES) return `${f.name} is ${(f.size / 1048576).toFixed(1)} MB; the limit is 2 MB.`
  return null
}

function SpecSlot(props: { step: number; title: string; hint: string; state: SlotState; onFile: (f: File) => void }) {
  const { step, title, hint, state, onFile } = props
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  const s = state.summary
  const tone = state.error || (s && !s.valid) ? 'bad' : s?.valid ? 'ok' : ''
  return (
    <section className={`slot${over ? ' slot--over' : ''}${tone ? ` slot--${tone}` : ''}`}
      onDragOver={(e) => { e.preventDefault(); setOver(true) }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files[0]; if (f) onFile(f) }}
      aria-label={title}>
      <header className="slot__head">
        <span className="slot__step">{step}</span>
        <div>
          <h2>{title}</h2>
          <p className="muted small">{hint}</p>
        </div>
      </header>
      <input ref={input} type="file" accept=".yaml,.yml,.json" hidden aria-label={`${title} file`}
        onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = '' }} />
      <div className="slot__drop" onClick={() => input.current?.click()} role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') input.current?.click() }}>
        <Icon name="file" size={22} />
        <span>{state.file ? <><strong>{state.file.name}</strong> · choose another</> : <>Drop a YAML or JSON file, or <u>browse</u></>}</span>
      </div>
      {state.checking && <p className="slot__status"><Spinner /> Validating {state.file?.name}…</p>}
      {state.error && <p className="slot__status error" role="alert">{state.error}</p>}
      {s && s.valid && (
        <div className="slot__status slot__status--ok">
          <p><Icon name="check" size={16} /> <strong>{s.title ?? 'Untitled API'}</strong> {s.api_version && <span className="muted">v{s.api_version}</span>}</p>
          <p className="muted small">{s.spec_format ?? `OpenAPI ${s.openapi_version}`} · {s.stats.paths} paths · {s.stats.operations} operations · {s.stats.schemas} schemas</p>
        </div>
      )}
      {s && !s.valid && (
        <div className="slot__status" role="alert">
          <p className="error"><strong>{s.filename}</strong> is not a valid Swagger 2.0 or OpenAPI 3.x document ({s.errors.length} problem{s.errors.length === 1 ? '' : 's'}).</p>
          <ul className="errors">
            {s.errors.slice(0, 6).map((e, i) => <li key={i}>{e.location && <code>{e.location}</code>} {e.message}</li>)}
          </ul>
          {s.errors.length > 6 && <p className="muted small">and {s.errors.length - 6} more</p>}
        </div>
      )}
    </section>
  )
}

function expected(s: Sample): { decision: Decision; after: Decision | null } | null {
  if (s.expected) return { decision: s.expected.decision, after: s.expected.decision_after_investigation }
  const m = s.description.match(/Expected: ([A-Z_]+)/)  // older backends: read it from ABOUT.txt
  if (!m || !['PROCEED', 'BLOCK', 'HUMAN_REVIEW'].includes(m[1])) return null
  return { decision: m[1] as Decision, after: /then BLOCK after investigation/.test(s.description) ? 'BLOCK' : null }
}

export function UploadView({ onResult }: { onResult: (a: Analysis) => void }) {
  const [base, setBase] = useState<SlotState>(empty)
  const [cand, setCand] = useState<SlotState>(empty)
  const [samples, setSamples] = useState<Sample[] | null>(null)
  const [samplesError, setSamplesError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadSamples = useCallback(() => {
    setSamplesError(null)
    api.samples().then(setSamples).catch((e) => setSamplesError(e.message))
  }, [])
  useEffect(loadSamples, [loadSamples])

  const check = (set: (s: SlotState) => void) => async (file: File) => {
    const pre = precheck(file)
    if (pre) { set({ file, summary: null, checking: false, error: pre }); return }
    set({ file, summary: null, checking: true, error: null })
    try {
      set({ file, summary: await api.validate(file), checking: false, error: null })
    } catch (e) {
      set({ file, summary: null, checking: false, error: (e as Error).message })
    }
  }

  const run = async (fn: () => Promise<Analysis>, label: string) => {
    setBusy(label); setError(null)
    try { onResult(await fn()) } catch (e) { setError((e as Error).message) } finally { setBusy(null) }
  }

  const ready = !!(base.summary?.valid && cand.summary?.valid && base.file && cand.file)
  const sameFile = ready && base.file!.name === cand.file!.name && base.file!.size === cand.file!.size

  return (
    <main className="page">
      <PageHeader title="New comparison"
        subtitle="Compare the API contract your clients use today (Swagger 2.0 or OpenAPI 3.0, 3.1, 3.2, in any combination) with the one you plan to release. Sentinel detects every structural change, runs a targeted compatibility test for each, and recommends Proceed, Review or Block. A person makes the final call." />

      <div className="slots">
        <SpecSlot step={1} title="Baseline: live today" hint="The contract existing clients were built against."
          state={base} onFile={check(setBase)} />
        <button type="button" className="slots__swap" title="Swap baseline and candidate" aria-label="Swap baseline and candidate"
          disabled={!base.file && !cand.file} onClick={() => { const b = base; setBase(cand); setCand(b) }}>
          <Icon name="swap" />
        </button>
        <SpecSlot step={2} title="Candidate: to release" hint="The contract you intend to ship."
          state={cand} onFile={check(setCand)} />
      </div>

      <div className="actionbar">
        <button type="button" className="btn btn--primary btn--lg" disabled={!ready || !!busy}
          onClick={() => run(() => api.analyse(base.file!, cand.file!), 'upload')}>
          {busy === 'upload' ? <><Spinner /> Comparing…</> : <><Icon name="play" size={16} /> Run comparison</>}
        </button>
        <span className="muted small">
          {ready ? (sameFile ? 'Both slots hold what looks like the same file.' : 'Both specs are valid.') : 'Add two valid specs to compare them.'}
        </span>
      </div>
      {error && <ErrorState title="Comparison failed" message={error} />}

      <section className="section">
        <h2 className="section__h">Sample pairs</h2>
        <p className="muted small section__sub">Bundled, offline examples with different expected outcomes and spec versions. Each ships a hand-written ground truth, and the result page scores the comparison against it. Some include consumer code for the investigation step.</p>
        {samplesError && <ErrorState title="Could not load samples" message={samplesError} onRetry={loadSamples} />}
        {!samples && !samplesError && <Loading label="Loading samples…" />}
        {samples && (
          <div className="cards">
            {samples.map((s) => {
              const exp = expected(s)
              const fmt = s.formats
              return (
                <article key={s.name} className="card">
                  <header className="card__head">
                    <code className="card__title">{s.name}</code>
                    {exp && <span className="card__exp">Expected <DecisionWord d={exp.decision} />
                      {exp.after && exp.after !== exp.decision && <>, then <DecisionWord d={exp.after} /> after investigation</>}</span>}
                  </header>
                  {fmt?.base && fmt.candidate && (
                    <p className="card__formats tiny">
                      <span className="pill">{fmt.base}</span> → <span className="pill">{fmt.candidate}</span>
                    </p>
                  )}
                  <p className="card__body">{s.description.replace(/\s*Expected:.*$/, '')}</p>
                  <footer className="card__foot">
                    <span className="muted tiny">
                      {s.context_files.length ? `${s.context_files.length} consumer file${s.context_files.length > 1 ? 's' : ''}` : 'No consumer files'}
                      {' · '}<a href={`/api/samples/${s.name}/base`} target="_blank" rel="noreferrer">baseline</a>
                      {' · '}<a href={`/api/samples/${s.name}/candidate`} target="_blank" rel="noreferrer">candidate</a>
                    </span>
                    <button type="button" className="btn btn--quiet" disabled={!!busy} aria-label={`Compare ${s.name}`}
                      onClick={() => run(() => api.analyseSample(s.name), s.name)}>
                      {busy === s.name ? <><Spinner size={13} /> Comparing…</> : 'Compare'}
                    </button>
                  </footer>
                </article>
              )
            })}
          </div>
        )}
      </section>
    </main>
  )
}
