/**
 * stateGraph.ts — turn an investigation trace into the agent's LangGraph state graph, with real run statistics.
 *
 * The topology is the one compiled in backend/app/agent/graph.py (StateGraph):
 *
 *   START → baseline → plan ─(queue empty)──────────────────────────────→ decide → END
 *                       └(queue)→ retrieve → assess → refine → execute → verify → reflect
 *                                    ↑                                           │
 *                                    └──── retry · next change · re-plan ────────┤
 *                                                                (done / stop) ──┘→ decide
 *
 * Every count shown on a node or an edge is computed from the recorded trace (consecutive events), never assumed.
 * Pure module: no React, no DOM, so it is unit-tested with plain Node (see stateGraph.test.ts).
 */
import type { TraceEvent } from '../../types'

export const LOOP = ['retrieve', 'assess', 'refine', 'execute', 'verify', 'reflect'] as const
export const STAGES = ['baseline', 'plan', ...LOOP, 'decide'] as const
export type Stage = (typeof STAGES)[number]
export type GraphNodeId = Stage | 'START' | 'END'

export const STAGE_LABEL: Record<string, string> = {
  START: 'Start', END: 'End', baseline: 'Baseline', plan: 'Plan', retrieve: 'Retrieve', assess: 'Assess',
  refine: 'Refine', execute: 'Execute', verify: 'Verify', reflect: 'Reflect', decide: 'Decide',
}

export const STAGE_ROLE: Record<string, string> = {
  baseline: 'Diff + contract tests',
  plan: 'Queue unsettled changes',
  retrieve: 'BM25 evidence search',
  assess: 'Judge consumer impact',
  refine: 'Derive targeted tests',
  execute: 'Run refined tests',
  verify: 'Check claims vs evidence',
  reflect: 'Retry, next or re-plan',
  decide: 'Final recommendation',
}

/** What each stage is, in one line, for tooltips and the side panel. */
export const STAGE_KIND: Record<string, 'io' | 'tool' | 'reason' | 'control'> = {
  START: 'io', END: 'io', baseline: 'tool', plan: 'control', retrieve: 'tool', assess: 'reason', refine: 'reason',
  execute: 'tool', verify: 'reason', reflect: 'control', decide: 'control',
}

export type EdgeId =
  | 'START>baseline' | 'baseline>plan' | 'plan>retrieve' | 'plan>decide' | 'retrieve>assess' | 'assess>refine'
  | 'refine>execute' | 'execute>verify' | 'verify>reflect' | 'reflect>retrieve' | 'reflect>decide' | 'decide>END'

export interface EdgeSpec {
  id: EdgeId
  source: GraphNodeId
  target: GraphNodeId
  conditional: boolean
  /** The routing condition in graph.py, for conditional edges. */
  condition?: string
}

/** The compiled graph's edges (graph.py: add_edge / add_conditional_edges). */
export const EDGES: EdgeSpec[] = [
  { id: 'START>baseline', source: 'START', target: 'baseline', conditional: false },
  { id: 'baseline>plan', source: 'baseline', target: 'plan', conditional: false },
  { id: 'plan>retrieve', source: 'plan', target: 'retrieve', conditional: true, condition: 'changes queued' },
  { id: 'plan>decide', source: 'plan', target: 'decide', conditional: true, condition: 'nothing to investigate' },
  { id: 'retrieve>assess', source: 'retrieve', target: 'assess', conditional: false },
  { id: 'assess>refine', source: 'assess', target: 'refine', conditional: false },
  { id: 'refine>execute', source: 'refine', target: 'execute', conditional: false },
  { id: 'execute>verify', source: 'execute', target: 'verify', conditional: false },
  { id: 'verify>reflect', source: 'verify', target: 'reflect', conditional: false },
  { id: 'reflect>retrieve', source: 'reflect', target: 'retrieve', conditional: true, condition: 'retry · next change · re-plan' },
  { id: 'reflect>decide', source: 'reflect', target: 'decide', conditional: true, condition: 'queue empty or budget spent' },
  { id: 'decide>END', source: 'decide', target: 'END', conditional: false },
]

export interface NodeStats {
  id: GraphNodeId
  visits: number
  totalMs: number
  modes: Record<string, number>      // tool | rules | llm -> count
  seqs: number[]                     // events of this node, in order
}

export interface EdgeStats {
  spec: EdgeSpec
  count: number
  /** reflect → retrieve only: how often each routing decision was taken. */
  decisions: Record<string, number>
}

export interface GraphFilter {
  changeId?: string
}

export interface StateGraphModel {
  nodes: Record<GraphNodeId, NodeStats>
  edges: EdgeStats[]
  /** Transitions in the trace that the compiled graph does not have (should be empty; shown, never hidden). */
  unexpected: { from: string; to: string; count: number }[]
  events: TraceEvent[]
  complete: boolean                  // the run reached decide
}

const ALL_NODES: GraphNodeId[] = ['START', ...STAGES, 'END']

function sorted(trace: TraceEvent[]): TraceEvent[] {
  return [...trace].sort((a, b) => a.seq - b.seq)
}

const decisionOf = (e: TraceEvent | undefined): string =>
  (e && e.node === 'reflect' && typeof e.detail?.decision === 'string') ? (e.detail.decision as string) : ''

/** Build the state graph with visit and transition counts, optionally for one change only. */
export function buildStateGraph(trace: TraceEvent[], filter: GraphFilter = {}): StateGraphModel {
  const events = sorted(trace)
  const cid = filter.changeId
  const belongs = (e: TraceEvent) => !cid || !e.change_id || e.change_id === cid
  const nodes = {} as Record<GraphNodeId, NodeStats>
  for (const id of ALL_NODES) nodes[id] = { id, visits: 0, totalMs: 0, modes: {}, seqs: [] }
  const edges = new Map<string, EdgeStats>(EDGES.map((s) => [s.id, { spec: s, count: 0, decisions: {} }]))
  const unexpected = new Map<string, number>()

  for (const e of events) {
    if (!belongs(e) || !(e.node in nodes)) continue
    const n = nodes[e.node as GraphNodeId]
    n.visits++
    n.totalMs += e.duration_ms || 0
    n.modes[e.mode] = (n.modes[e.mode] ?? 0) + 1
    n.seqs.push(e.seq)
  }

  const path: string[] = ['START', ...events.map((e) => e.node)]
  const complete = events.some((e) => e.node === 'decide')
  if (complete) path.push('END')
  const at = (i: number): TraceEvent | undefined => (i >= 1 && i <= events.length ? events[i - 1] : undefined)
  for (let i = 0; i < path.length - 1; i++) {
    const a = at(i), b = at(i + 1)
    // with a change filter, keep the transitions that enter, stay in or leave that change
    if (cid && !((a && a.change_id === cid) || (b && b.change_id === cid) || (!a?.change_id && !b?.change_id))) continue
    const key = `${path[i]}>${path[i + 1]}`
    const st = edges.get(key)
    if (!st) { unexpected.set(key, (unexpected.get(key) ?? 0) + 1); continue }
    st.count++
    if (key === 'reflect>retrieve') {
      const d = decisionOf(a) || 'next'
      st.decisions[d] = (st.decisions[d] ?? 0) + 1
    }
  }
  if (cid) { nodes.START.visits = 0; nodes.END.visits = 0 }
  else { nodes.START.visits = events.length ? 1 : 0; nodes.END.visits = complete ? 1 : 0 }

  return {
    nodes, edges: [...edges.values()], events, complete,
    unexpected: [...unexpected.entries()].map(([k, count]) => ({ from: k.split('>')[0], to: k.split('>')[1], count })),
  }
}

/** Index of the stage reached at step `upTo` (inclusive), for replay: which node is active, which edges are walked. */
export function replayAt(trace: TraceEvent[], upTo: number): { active: GraphNodeId | null; walked: Set<EdgeId>; event?: TraceEvent } {
  const events = sorted(trace).slice(0, Math.max(0, upTo + 1))
  const walked = new Set<EdgeId>()
  const path = ['START', ...events.map((e) => e.node)]
  for (let i = 0; i < path.length - 1; i++) {
    const k = `${path[i]}>${path[i + 1]}` as EdgeId
    if (EDGES.some((s) => s.id === k)) walked.add(k)
  }
  const last = events[events.length - 1]
  return { active: (last?.node as GraphNodeId) ?? null, walked, event: last }
}

/* ------------------------------------------------------------------ runs (per change, per attempt) */

export interface RunAttempt {
  changeId: string
  attempt: number
  cells: Partial<Record<(typeof LOOP)[number], TraceEvent>>
  decision: string                    // reflect decision that ended the attempt
}

export interface RunsModel {
  setup: TraceEvent[]                 // baseline, plan
  attempts: RunAttempt[]
  final?: TraceEvent                  // decide
}

/** Group the loop events into one row per change attempt (a retry starts a new attempt for the same change). */
export function buildRuns(trace: TraceEvent[]): RunsModel {
  const out: RunsModel = { setup: [], attempts: [] }
  let cur: RunAttempt | null = null
  const tries = new Map<string, number>()
  for (const e of sorted(trace)) {
    if (e.node === 'baseline' || e.node === 'plan') { out.setup.push(e); continue }
    if (e.node === 'decide') { out.final = e; continue }
    if (!(LOOP as readonly string[]).includes(e.node)) continue
    const id = e.change_id ?? '—'
    if (e.node === 'retrieve' || !cur || cur.changeId !== id) {
      const n: number = (tries.get(id) ?? 0) + 1
      tries.set(id, n)
      cur = { changeId: id, attempt: n, cells: {}, decision: '' }
      out.attempts.push(cur)
    }
    cur.cells[e.node as (typeof LOOP)[number]] = e
    if (e.node === 'reflect') cur.decision = decisionOf(e)
  }
  return out
}

/** A short, factual result for one cell of the runs table (from the event's own detail). */
export function cellResult(e: TraceEvent): string {
  const d = (e.detail ?? {}) as Record<string, unknown>
  const len = (k: string) => (Array.isArray(d[k]) ? (d[k] as unknown[]).length : null)
  switch (e.node) {
    case 'retrieve': {
      const hits = len('hits')
      return hits === null ? 'searched' : `${hits} hit${hits === 1 ? '' : 's'}${d.strategy === 'broadened' ? ' · broad' : ''}`
    }
    case 'assess': {
      const f = len('findings')
      const m = /Impact:\s*([\w_-]+)/.exec(e.summary)
      return `${m ? m[1].replace(/_/g, ' ') : 'assessed'}${f ? ` · ${f} finding${f === 1 ? '' : 's'}` : ''}`
    }
    case 'refine': {
      const t = len('tests')
      return t === null ? 'refined' : `${t} test${t === 1 ? '' : 's'}`
    }
    case 'execute': {
      const r = Array.isArray(d.results) ? (d.results as { status: string }[]) : []
      if (!r.length) return 'nothing to run'
      const failed = r.filter((x) => x.status === 'failed').length
      return failed ? `${failed}/${r.length} failed` : `${r.length} passed`
    }
    case 'verify': {
      const v = Array.isArray(d.verification) ? (d.verification as { verified: boolean }[]) : []
      const m = /Outcome:\s*([\w_]+)/.exec(e.summary)
      const ok = v.filter((x) => x.verified).length
      return v.length ? `${ok}/${v.length} verified` : (m ? m[1].replace(/_/g, ' ') : 'checked')
    }
    case 'reflect':
      return decisionOf(e) || 'done'
    default:
      return e.summary
  }
}

/** Tone of a cell, so the table reads at a glance. */
export function cellTone(e: TraceEvent): 'bad' | 'warn' | 'ok' | 'muted' {
  const d = (e.detail ?? {}) as Record<string, unknown>
  if (e.node === 'execute') {
    const r = Array.isArray(d.results) ? (d.results as { status: string }[]) : []
    return r.some((x) => x.status === 'failed') ? 'bad' : r.length ? 'ok' : 'muted'
  }
  if (e.node === 'verify') {
    const v = Array.isArray(d.verification) ? (d.verification as { verified: boolean }[]) : []
    return v.some((x) => x.verified) ? 'ok' : v.length ? 'warn' : 'muted'
  }
  if (e.node === 'reflect') {
    const dec = decisionOf(e)
    return dec === 'retry' ? 'warn' : dec === 'replan' ? 'bad' : 'muted'
  }
  if (e.node === 'retrieve') return Array.isArray(d.hits) && d.hits.length ? 'ok' : 'muted'
  return 'muted'
}

/** Unique change IDs in order of first appearance. */
export function traceChangeIds(trace: TraceEvent[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const e of sorted(trace)) if (e.change_id && !seen.has(e.change_id)) { seen.add(e.change_id); out.push(e.change_id) }
  return out
}

export const fmtMs = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} s` : `${Math.round(ms)} ms`)
