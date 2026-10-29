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
  if (!EXT.test(f.name)) return `${f.name} is not a .yaml, .yml or .json file. Only OpenAPI specifications in JSON (.json) or YAML (.yaml, .yml) format are accepted.`
  if (f.size === 0) return `${f.name} is empty.`
  if (f.size > MAX_BYTES) return `${f.name} is ${(f.size / 1048576).toFixed(1)} MB; the limit is 2 MB.`
  return null
}

function SpecSlot(props: { step: number; title: string; hint: string; formatHint: string; state: SlotState; onFile: (f: File) => void }) {
  const { step, title, hint, formatHint, state, onFile } = props
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
      <p className="muted tiny" style={{ margin: '0.1rem 0 0' }}>{formatHint}</p>
      {state.checking && <p className="slot__status"><Spinner /> Validating {state.file?.name}…</p>}
      {state.error && <p className="slot__status error" role="alert">{state.error}</p>}
      {s && s.valid && (
        <div className="slot__status slot__status--ok">
          <p><Icon name="check" size={16} /> <strong>{s.title ?? 'Untitled API'}</strong> {s.api_version && <span className="muted">v{s.api_version}</span>}</p>
          <p className="muted small">OpenAPI {s.openapi_version} · {s.stats.paths} paths · {s.stats.operations} operations · {s.stats.schemas} schemas</p>
        </div>
      )}
      {s && !s.valid && (
        <div className="slot__status" role="alert">
          <p className="error"><strong>{s.filename}</strong> is not a valid OpenAPI 3.0/3.1 document ({s.errors.length} problem{s.errors.length === 1 ? '' : 's'}).</p>
          <ul className="errors">
            {s.errors.slice(0, 6).map((e, i) => <li key={i}>{e.location && <code>{e.location}</code>} {e.message}</li>)}
          </ul>
          {s.errors.length > 6 && <p className="muted small">and {s.errors.length - 6} more</p>}
        </div>
      )}
    </section>
  )
}

function expected(desc: string): Decision | null {
  const m = desc.match(/Expected: ([A-Z_]+)/)
  if (!m) return null
  const last = m[1]
  return (['PROCEED', 'BLOCK', 'HUMAN_REVIEW'].includes(last) ? last : null) as Decision | null
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
        subtitle="Compare your current and proposed OpenAPI contracts. Sentinel detects structural changes, runs compatibility tests, and recommends Proceed, Review or Block. An optional investigation uses the LangGraph agent to search consumer code for evidence of impact." />

      <div className="slots">
        <SpecSlot step={1} title="Baseline specification — Current API"
          hint="The OpenAPI contract existing clients were built against. This is the API version live in production today."
          formatHint="OpenAPI 3.0.x or 3.1.x · JSON (.json) or YAML (.yaml, .yml) · max 2 MB · e.g. orders-api-v2.yaml"
          state={base} onFile={check(setBase)} />
        <button type="button" className="slots__swap" title="Swap baseline and candidate" aria-label="Swap baseline and candidate"
          disabled={!base.file && !cand.file} onClick={() => { const b = base; setBase(cand); setCand(b) }}>
          <Icon name="swap" />
        </button>
        <SpecSlot step={2} title="Candidate specification — Proposed API"
          hint="The updated OpenAPI contract being evaluated for release. This is the version you intend to ship."
          formatHint="OpenAPI 3.0.x or 3.1.x · JSON (.json) or YAML (.yaml, .yml) · max 2 MB · e.g. orders-api-v3.yaml"
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

      <div className="callout callout--muted" style={{ marginTop: '1rem', fontSize: '0.82rem' }}>
        <p style={{ margin: '0 0 0.4rem' }}>
          <strong>What the analysis does:</strong> Structural comparison of every path, operation, parameter, schema and response between the two specs;
          a targeted contract test for each detected change; and a release recommendation (Proceed, Review, or Block).
          After comparison, you can optionally run an <strong>investigation</strong> that uses the LangGraph agent with BM25 evidence retrieval
          over consumer code to assess real-world impact.
        </p>
        <p style={{ margin: '0 0 0.4rem' }}>
          <strong>Accepted inputs:</strong> Valid OpenAPI 3.0.x or 3.1.x specification files in JSON or YAML format.
          PDFs, screenshots, Word documents, source-code files, Swagger 2.0, AsyncAPI, and other API formats are <strong>not accepted</strong>.
          Each file must be a single bundled document (external <code>$ref</code>s are rejected).
        </p>
        <p style={{ margin: '0 0 0.4rem' }}>
          <strong>Server-side processing:</strong> Uploaded files are sent to the API Sentinel backend for validation,
          comparison, contract testing, and optional investigation. Browser-side validation is preliminary; the server
          performs the authoritative checks. In the standard local setup, the backend runs on your own machine.
        </p>
        <p style={{ margin: 0 }}>
          <strong>Privacy notice:</strong> API Sentinel processes uploaded specifications on the local application server.
          Specifications and analysis results are stored in the local SQLite database so previous analyses can be reopened.
          Consumer-code context (uploaded during investigation) is processed in memory and is not saved to that database.
          AI-assisted investigation uses a locally running Ollama model rather than a hosted LLM API.
          Avoid uploading credentials, secrets, personal data, or proprietary material unless you are authorised to do so.
        </p>
      </div>

      <section className="section">
        <h2 className="section__h">Sample pairs</h2>
        <p className="muted small section__sub">Bundled, offline examples with different expected outcomes. Some include consumer code for the investigation step. Use these if you do not have your own specifications.</p>
        {samplesError && <ErrorState title="Could not load samples" message={samplesError} onRetry={loadSamples} />}
        {!samples && !samplesError && <Loading label="Loading samples…" />}
        {samples && (
          <div className="cards">
            {samples.map((s) => {
              const exp = expected(s.description)
              return (
                <article key={s.name} className="card">
                  <header className="card__head">
                    <code className="card__title">{s.name}</code>
                    {exp && <span className="card__exp">Expected <DecisionWord d={exp} />
                      {/then BLOCK after investigation/.test(s.description) && <>, then <DecisionWord d="BLOCK" /> after investigation</>}</span>}
                  </header>
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
