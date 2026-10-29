import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { api } from '../api'
import type { ChainStatus, LlmCheck, LlmMode, LlmStatus, SelfTest, ServerConfig } from '../types'
import { Icon, Spinner } from '../ui'

export type Page = 'compare' | 'analysis' | 'history' | 'audit' | 'evaluation'

const NAV: { href: string; label: string; page: Page; icon: string }[] = [
  { href: '#', label: 'New comparison', page: 'compare', icon: 'compare' },
  { href: '#/history', label: 'History', page: 'history', icon: 'history' },
  { href: '#/audit', label: 'Audit log', page: 'audit', icon: 'audit' },
  { href: '#/evaluation', label: 'Evaluation', page: 'evaluation', icon: 'chart' },
]

function Dot({ tone }: { tone: 'ok' | 'bad' | 'warn' | 'muted' }) {
  return <span className={`dot dot--${tone}`} aria-hidden="true" />
}

type Verdict = 'passed' | 'failed' | 'unavailable' | 'not tested'

const MODE_TEXT: Record<LlmMode, string> = {
  llm_assisted: 'completed, LLM-assisted',
  partial_fallback: 'completed, partly rules fallback',
  rules_fallback: 'completed with rules fallback (LLM unavailable)',
  rules_only: 'completed, rules only',
  llm_not_needed: 'completed, LLM not needed',
}

function V({ v }: { v: Verdict }) {
  const tone = v === 'passed' ? 'ok' : v === 'failed' ? 'error' : v === 'unavailable' ? 'warn' : 'muted'
  return <span className={`verdictchip verdictchip--${tone}`}>{v}</span>
}

/** Diagnostics from GET /api/llm/status. Nothing is shown as passed unless a request actually succeeded. */
function LlmDiagnostics({ st }: { st: LlmStatus }) {
  const rt = st.runtime
  const step = (id: string) => st.last.selftest?.steps.find((x) => x.id === id)
  const fromStep = (id: string): Verdict => {
    const x = step(id)
    return !x ? 'not tested' : x.ok ? 'passed' : x.skipped ? 'not tested' : x.category === 'unavailable' ? 'unavailable' : 'failed'
  }
  const reach: Verdict = !rt ? 'not tested' : rt.reachable ? 'passed' : 'unavailable'
  const present: Verdict = !rt ? 'not tested' : !rt.reachable ? 'unavailable' : rt.model_present ? 'passed' : 'failed'
  const c = st.last.check
  const a = step('A')
  const recent = c && (!st.last.selftest || c.at >= st.last.selftest.at) ? c : null
  const infer: Verdict = recent ? (recent.ok ? 'passed' : recent.category === 'unavailable' ? 'unavailable' : 'failed')
    : a ? fromStep('A') : 'not tested'
  const inv = st.last.investigation
  const tools: Verdict = step('C') ? fromStep('C') : inv?.tool_calls ? 'passed' : 'not tested'
  const anyMocked = [c?.live, st.last.selftest?.live, inv?.live].some((x) => x === false)
  return (
    <dl className="diag tiny" aria-label="LLM diagnostics">
      {rt && <div><dt>Endpoint</dt><dd><code>{rt.endpoint}</code> (local)</dd></div>}
      {rt && <div><dt>Ollama reachable</dt><dd><V v={reach} />{rt.version ? ` v${rt.version}` : ''}</dd></div>}
      {rt && <div><dt>Model installed</dt><dd><V v={present} />{rt.capabilities ? ` ${rt.capabilities.join(', ')}` : ''}</dd></div>}
      {rt?.error && <p className="error">{rt.error}</p>}
      {rt && rt.reachable && rt.model_present === false && rt.installed_models.length > 0 &&
        <p className="muted">Installed: {rt.installed_models.join(', ')}</p>}
      <div><dt>Last inference</dt><dd><V v={infer} /></dd></div>
      <div><dt>Structured assessment</dt><dd><V v={fromStep('B')} /></dd></div>
      <div><dt>Tool calling</dt><dd><V v={tools} /></dd></div>
      <div><dt>Last investigation</dt><dd>{!inv ? <V v="not tested" /> : inv.status === 'failed'
        ? <V v="failed" /> : <span className={inv.mode === 'rules_fallback' ? 'warntext' : ''}>{inv.mode ? MODE_TEXT[inv.mode] : 'completed'}</span>}</dd></div>
      {anyMocked && <p className="error">Some results came from a test transport, not real inference.</p>}
      <p className="muted">Since the server started. “Run self-test” exercises inference, structured output and tool calling.</p>
    </dl>
  )
}

function SystemStatus({ page }: { page: Page }) {
  const [cfg, setCfg] = useState<ServerConfig | null>(null)
  const [up, setUp] = useState<boolean | null>(null)
  const [chain, setChain] = useState<ChainStatus | null>(null)
  const [check, setCheck] = useState<LlmCheck | null>(null)
  const [checking, setChecking] = useState(false)
  const [selftest, setSelftest] = useState<SelfTest | null>(null)
  const [testing, setTesting] = useState(false)
  const [status, setStatus] = useState<LlmStatus | null>(null)
  const loadStatus = useCallback(() => { api.llmStatus().then(setStatus).catch(() => setStatus(null)) }, [])
  const runSelfTest = async () => {
    setTesting(true)
    try { setSelftest(await api.llmSelfTest()) } catch (e) {
      setSelftest({ configured: true, ok: false, steps: [], message: (e as Error).message })
    } finally { setTesting(false); loadStatus() }
  }

  const refresh = useCallback(() => {
    api.health().then(() => setUp(true)).catch(() => setUp(false))
    api.config().then(setCfg).catch(() => setCfg(null))
    api.verifyAudit().then(setChain).catch(() => setChain(null))
    loadStatus()
  }, [loadStatus])
  useEffect(() => {
    refresh()
    const t = window.setInterval(refresh, 30000)
    return () => window.clearInterval(t)
  }, [refresh, page])

  const testLlm = async () => {
    setChecking(true)
    try { setCheck(await api.llmCheck()); api.config().then(setCfg).catch(() => undefined) } catch (e) {
      setCheck({ configured: true, ok: false, message: (e as Error).message, category: 'request_failed' })
    } finally { setChecking(false); loadStatus() }
  }

  const llm = cfg?.llm
  // most recent real result: this tab's check, else the server's last check / self-test (since it started)
  const srv = status?.last
  const lastOk: boolean | null = check ? check.ok
    : srv?.check && (!srv.selftest || srv.check.at >= srv.selftest.at) ? srv.check.ok
      : srv?.selftest ? srv.selftest.steps[0]?.ok ?? null : null
  return (
    <section className="sysstatus" aria-label="System status">
      <p className="sysstatus__h">Status</p>
      <p className="sysstatus__row">
        <Dot tone={up === null ? 'muted' : up ? 'ok' : 'bad'} />
        <span>Backend {up === null ? '…' : up ? 'online' : 'offline'}{cfg?.version ? ` · v${cfg.version}` : ''}</span>
      </p>
      <p className="sysstatus__row">
        <Dot tone={!cfg ? 'muted' : cfg.reasoning ? (lastOk === null ? 'warn' : lastOk ? 'ok' : 'bad') : 'muted'} />
        <span>
          {!cfg ? 'Reasoning …' : cfg.reasoning
            ? <>LLM <code>{llm?.model ?? cfg.reasoning}</code>{lastOk === null ? ' · not tested' : lastOk ? ' · working' : ' · failing'}</>
            : llm?.configuration_problem ? <span className="error">{llm.configuration_problem}</span> : 'Rules only'}
        </span>
      </p>
      <p className="sysstatus__row">
        <Dot tone={!chain ? 'muted' : chain.valid ? 'ok' : 'bad'} />
        <span>{!chain ? 'Audit …' : chain.valid ? `Audit intact (${chain.events})` : `Chain broken #${chain.broken_at}`}</span>
      </p>
      {cfg?.reasoning && (
        <details className="sysstatus__details">
          <summary>LLM diagnostics</summary>
          <div className="sysstatus__llm">
            <button type="button" className="btn btn--tiny" onClick={testLlm} disabled={checking}>
              {checking ? <><Spinner size={11} /> Testing…</> : 'Test connection'}
            </button>
            <button type="button" className="btn btn--tiny" onClick={runSelfTest} disabled={testing}
              title="Tests A-C: basic inference, structured assessment, tool calling (a few real requests)">
              {testing ? <><Spinner size={11} /> Self-test…</> : 'Run self-test'}
            </button>
            {selftest && (
              <div className="tiny selftest" role="status">
                {selftest.message && <p className="error">{selftest.message}</p>}
                {selftest.steps.map((st) => (
                  <p key={st.id} className={st.ok ? 'ok' : st.skipped ? '' : 'error'}>
                    {st.ok ? '✓' : st.skipped ? '–' : '✗'} {st.id} {st.name}: {st.detail}
                  </p>
                ))}
                {selftest.steps.length > 0 && <p>{selftest.live ? 'Real requests (not a test transport)' : 'NOT live: test transport'} to {selftest.model}</p>}
              </div>
            )}
            {status && <LlmDiagnostics st={status} />}
            {check && (
              <p className={`tiny ${check.ok ? 'ok' : 'error'}`} role="status">
                {check.ok ? `Reply from ${check.resolved_model ?? check.model} in ${check.latency_ms} ms.` : `${check.category}: ${check.message}`}
                {check.ok && check.model_note && <><br />{check.model_note}</>}
              </p>
            )}
          </div>
        </details>
      )}
    </section>
  )
}

export function Shell({ page, analysisId, children }: { page: Page; analysisId: string | null; children: ReactNode }) {
  return (
    <div className="shell">
      <aside className="sidebar">
        <a href="#" className="brand">
          <span className="brand__mark" aria-hidden="true"><Icon name="check" size={14} /></span>
          <span>
            <span className="brand__name">API Sentinel</span>
            <span className="brand__sub">Release gate for OpenAPI 3.0/3.1</span>
          </span>
        </a>
        <nav className="nav" aria-label="Main">
          {NAV.map((n) => (
            <a key={n.page} href={n.href} className={`nav__link${page === n.page ? ' nav__link--on' : ''}`}
              aria-current={page === n.page ? 'page' : undefined}>
              <Icon name={n.icon} /> {n.label}
            </a>
          ))}
          {analysisId && (
            <a href={`#/analysis/${analysisId}`} className={`nav__link nav__link--sub${page === 'analysis' ? ' nav__link--on' : ''}`}
              aria-current={page === 'analysis' ? 'page' : undefined}>
              <Icon name="file" /> Analysis <code>{analysisId.slice(0, 6)}</code>
            </a>
          )}
        </nav>
        <SystemStatus page={page} />
      </aside>
      <div className="main">{children}</div>
    </div>
  )
}
