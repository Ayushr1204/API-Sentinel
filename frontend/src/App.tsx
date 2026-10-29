import { useEffect, useState } from 'react'
import { api } from './api'
import { AuditView, EvaluationView, HistoryView } from './components/Pages'
import { ResultsView, type ResultTab } from './components/ResultsView'
import { Shell, type Page } from './components/Shell'
import { UploadView } from './components/UploadView'
import type { Analysis } from './types'
import { ErrorState, Loading } from './ui'

type Route = { page: Page; id?: string; tab?: ResultTab }
const TABS: ResultTab[] = ['changes', 'investigation', 'decision', 'audit']

function parse(): Route {
  const h = window.location.hash
  const m = h.match(/^#\/analysis\/([\w-]+)(?:\/(\w+))?$/)
  if (m) return { page: 'analysis', id: m[1], tab: TABS.includes(m[2] as ResultTab) ? (m[2] as ResultTab) : 'changes' }
  if (h === '#/history' || h === '#/audit' || h === '#/evaluation') return { page: h.slice(2) as Page }
  return { page: 'compare' }
}

export default function App() {
  const [route, setRoute] = useState<Route>(parse)
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    const onHash = () => setRoute(parse())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const wantedId = route.page === 'analysis' ? route.id ?? null : null
  useEffect(() => {
    if (!wantedId || analysis?.id === wantedId) return
    setLoadError(null)
    api.get(wantedId).then(setAnalysis).catch((e) => setLoadError(e.message))
  }, [wantedId, analysis?.id, reload])

  useEffect(() => {
    const titles: Record<Page, string> = { compare: 'New comparison', analysis: 'Analysis', history: 'History',
      audit: 'Audit log', evaluation: 'Evaluation' }
    document.title = `${titles[route.page]} · API Sentinel`
  }, [route.page])

  const show = (a: Analysis) => { setAnalysis(a); window.location.hash = `/analysis/${a.id}` }
  const setTab = (t: ResultTab) => { if (analysis) window.location.hash = `/analysis/${analysis.id}${t === 'changes' ? '' : `/${t}`}` }

  return (
    <Shell page={route.page} analysisId={analysis?.id ?? null}>
      {route.page === 'analysis' && analysis && analysis.id === route.id && (
        <ResultsView key={analysis.id} a={analysis} tab={route.tab ?? 'changes'} onTab={setTab} onChange={setAnalysis} />
      )}
      {route.page === 'analysis' && analysis?.id !== route.id && (
        loadError
          ? <div className="page"><ErrorState title="Could not open this analysis" message={loadError} onRetry={() => setReload((n) => n + 1)} /></div>
          : <div className="page"><Loading label="Loading analysis…" /></div>
      )}
      {route.page === 'compare' && <UploadView onResult={show} />}
      {route.page === 'history' && <HistoryView />}
      {route.page === 'audit' && <AuditView />}
      {route.page === 'evaluation' && <EvaluationView />}
    </Shell>
  )
}
