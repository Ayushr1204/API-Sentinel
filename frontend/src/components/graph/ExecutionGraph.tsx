import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import {
  BaseEdge, Controls, EdgeLabelRenderer, Handle, Position, ReactFlow, ReactFlowProvider, getSmoothStepPath, useReactFlow,
  type Edge, type EdgeProps, type EdgeTypes, type Node, type NodeProps, type NodeTypes,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { FINDING_LABEL, MODE_LABEL, OUTCOME_LABEL } from '../../labels'
import type { AgentRecord, TraceEvent } from '../../types'
import { StatusBadge } from '../../ui'
import {
  LOOP, STAGE_KIND, STAGE_LABEL, STAGE_ROLE, buildRuns, buildStateGraph, cellResult, cellTone, fmtMs,
  replayAt, traceChangeIds, type EdgeId, type EdgeStats, type GraphNodeId, type NodeStats,
} from './stateGraph'

/* ================================================================ layout (fixed: it mirrors graph.py) */

const W = 172, H = 84, PILL_W = 76, PILL_H = 36
const COL = { baseline: 0, ring0: 240, ring1: 472, ring2: 704, decide: 956 }
const ROW = { io: -96, top: 0, ring0: 170, ring1: 320 }
const LOOP_OFFSET = 36, RIGHT_OFFSET = 30, BOTTOM_OFFSET = 34

const POS: Record<GraphNodeId, { x: number; y: number }> = {
  START: { x: COL.baseline + (W - PILL_W) / 2, y: ROW.io },
  baseline: { x: COL.baseline, y: ROW.top },
  plan: { x: COL.ring0, y: ROW.top },
  retrieve: { x: COL.ring0, y: ROW.ring0 },
  assess: { x: COL.ring1, y: ROW.ring0 },
  refine: { x: COL.ring2, y: ROW.ring0 },
  execute: { x: COL.ring2, y: ROW.ring1 },
  verify: { x: COL.ring1, y: ROW.ring1 },
  reflect: { x: COL.ring0, y: ROW.ring1 },
  decide: { x: COL.decide, y: ROW.top },
  END: { x: COL.decide + (W - PILL_W) / 2, y: ROW.io },
}

type Side = 't' | 'b' | 'l' | 'r'
const ROUTE: Record<EdgeId, [Side, Side, number?]> = {
  'START>baseline': ['b', 't'], 'baseline>plan': ['r', 'l'], 'plan>retrieve': ['b', 't'], 'plan>decide': ['r', 'l'],
  'retrieve>assess': ['r', 'l'], 'assess>refine': ['r', 'l'], 'refine>execute': ['r', 'r', RIGHT_OFFSET],
  'execute>verify': ['l', 'r'], 'verify>reflect': ['l', 'r'], 'reflect>retrieve': ['l', 'l', LOOP_OFFSET],
  'reflect>decide': ['b', 'b', BOTTOM_OFFSET], 'decide>END': ['t', 'b'],
}

/** Where each edge label sits (flow coordinates) and which way it hangs off that point. */
const LABEL_AT: Partial<Record<EdgeId, { x: number; y: number; anchor: 'center' | 'left' | 'right' }>> = {
  'plan>decide': { x: (COL.ring0 + W + COL.decide) / 2, y: ROW.top + H / 2, anchor: 'center' },
  'plan>retrieve': { x: COL.ring0 + W / 2, y: (ROW.top + H + ROW.ring0) / 2, anchor: 'center' },
  'reflect>retrieve': { x: COL.ring0 - LOOP_OFFSET - 10, y: (ROW.ring0 + ROW.ring1 + H) / 2, anchor: 'right' },
  'reflect>decide': { x: (COL.ring0 + W / 2 + COL.decide + W / 2) / 2, y: ROW.ring1 + H + BOTTOM_OFFSET, anchor: 'center' },
  'refine>execute': { x: COL.ring2 + W + RIGHT_OFFSET + 8, y: (ROW.ring0 + ROW.ring1 + H) / 2, anchor: 'left' },
}

const DECISION_LABEL: Record<string, string> = { retry: 'retry', next: 'next change', replan: 're-plan', stop: 'stop' }

/* ================================================================ nodes */

interface StageData extends Record<string, unknown> {
  id: GraphNodeId
  stats: NodeStats
  active: boolean
  dim: boolean
}
type StageNode = Node<StageData, 'stage'>

function Handles() {
  const sides: [Side, Position][] = [['t', Position.Top], ['b', Position.Bottom], ['l', Position.Left], ['r', Position.Right]]
  return (
    <>
      {sides.map(([s, p]) => <Handle key={`s${s}`} id={`s-${s}`} type="source" position={p} className="sg-handle" isConnectable={false} />)}
      {sides.map(([s, p]) => <Handle key={`t${s}`} id={`t-${s}`} type="target" position={p} className="sg-handle" isConnectable={false} />)}
    </>
  )
}

function StageCard({ data, selected }: NodeProps<StageNode>) {
  const { id, stats, active, dim } = data
  if (id === 'START' || id === 'END') {
    return (
      <div className={`sg-pill${dim ? ' is-dim' : ''}${active ? ' is-active' : ''}`} style={{ width: PILL_W, height: PILL_H }}>
        <Handles />{STAGE_LABEL[id]}
      </div>
    )
  }
  const modes = Object.entries(stats.modes).sort((a, b) => b[1] - a[1])
  const kind = STAGE_KIND[id]
  return (
    <div className={`sg-node sg-node--${kind}${selected ? ' is-sel' : ''}${active ? ' is-active' : ''}${dim ? ' is-dim' : ''}`}
      style={{ width: W, height: H }} title={`${STAGE_LABEL[id]}: ${STAGE_ROLE[id]}`}>
      <Handles />
      <div className="sg-node__head">
        <span className="sg-node__name">{STAGE_LABEL[id]}</span>
        <span className="sg-node__visits" aria-label={`${stats.visits} visits`}>{stats.visits ? `×${stats.visits}` : '—'}</span>
      </div>
      <div className="sg-node__role">{STAGE_ROLE[id]}</div>
      <div className="sg-node__meta">
        {modes.length
          ? modes.map(([m, n]) => <span key={m} className={`sg-mode sg-mode--${m}`}>{MODE_LABEL[m] ?? m} {n}</span>)
          : <span className="sg-mode">not reached</span>}
        {stats.visits > 0 && <span className="sg-node__ms">{fmtMs(stats.totalMs)}</span>}
      </div>
    </div>
  )
}

/** Invisible node that keeps the bottom edge label inside the fitted view. */
function Spacer() { return <div style={{ width: 1, height: 1 }} aria-hidden="true" /> }

const nodeTypes: NodeTypes = { stage: StageCard as NodeTypes[string], spacer: Spacer }

/* ================================================================ edges */

interface FlowData extends Record<string, unknown> {
  stats: EdgeStats
  walked: boolean
  replaying: boolean
  offset?: number
  sourceVisits: number
}
type FlowEdge = Edge<FlowData, 'flow'>

function FlowEdgeView(p: EdgeProps<FlowEdge>) {
  const { stats, walked, replaying, offset, sourceVisits } = p.data!
  const [path, lx, ly] = getSmoothStepPath({
    sourceX: p.sourceX, sourceY: p.sourceY, sourcePosition: p.sourcePosition,
    targetX: p.targetX, targetY: p.targetY, targetPosition: p.targetPosition,
    borderRadius: 14, offset: offset ?? 20,
  })
  const id = stats.spec.id
  const used = replaying ? walked : stats.count > 0
  const retry = id === 'reflect>retrieve' && (stats.decisions.retry ?? 0) > 0
  const tone = !used ? 'off' : retry ? 'retry' : 'on'
  const width = used ? 1.6 + Math.min(2.4, Math.log2(stats.count + 1) * 0.55) : 1.2
  const stroke = tone === 'on' ? 'var(--accent)' : tone === 'retry' ? 'var(--review)' : 'var(--line-strong)'
  const style: CSSProperties = { stroke, strokeWidth: width, strokeDasharray: stats.spec.conditional ? '6 4' : undefined, opacity: tone === 'off' ? 0.85 : 1 }
  const at = LABEL_AT[id] ?? { x: lx, y: ly, anchor: 'center' as const }
  const shift = at.anchor === 'right' ? '-100%' : at.anchor === 'left' ? '0%' : '-50%'
  const decisions = Object.entries(stats.decisions)
  // sequential edges carry the same count as their source node; label them only when that is not the case
  const showLabel = stats.spec.conditional || (stats.count > 0 && stats.spec.source !== 'START' && stats.count !== sourceVisits)
  return (
    <>
      <BaseEdge id={p.id} path={path} className={`sg-edge sg-edge--${tone}`} style={style} markerEnd={`url(#sg-arrow-${tone})`} />
      {showLabel && (
        <EdgeLabelRenderer>
          <div className={`sg-elabel sg-elabel--${tone}`} style={{ transform: `translate(${shift}, -50%) translate(${at.x}px, ${at.y}px)` }}>
            {stats.spec.condition && <span className="sg-elabel__cond">{stats.spec.condition}</span>}
            {decisions.length > 0
              ? <span className="sg-elabel__counts">{decisions.map(([d, n]) => (
                <span key={d} className={`sg-dec sg-dec--${d}`}>{DECISION_LABEL[d] ?? d} ×{n}</span>))}</span>
              : <span className="sg-elabel__n">{stats.count ? `×${stats.count}` : 'not taken'}</span>}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}

const edgeTypes: EdgeTypes = { flow: FlowEdgeView as EdgeTypes[string] }

function Markers() {
  return (
    <svg className="sg-defs" aria-hidden="true" width="0" height="0">
      <defs>
        {(['on', 'off', 'retry'] as const).map((t) => (
          <marker key={t} id={`sg-arrow-${t}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
            orient="auto-start-reverse" markerUnits="userSpaceOnUse">
            <path d="M0,0 L10,5 L0,10 z" className={`sg-arrow sg-arrow--${t}`} />
          </marker>
        ))}
      </defs>
    </svg>
  )
}

/* ================================================================ event detail (kept from the first graph view) */

export function EventDetail({ event, onShowInTrace, onPick }: {
  event: TraceEvent; onShowInTrace?: (seq: number) => void; onPick?: (id: string) => void
}) {
  const d = event.detail as Record<string, unknown>
  const hits = d.hits as { id: string; citation: string; score: number }[] | undefined
  const results = d.results as { test_id: string; status: string; summary: string }[] | undefined
  const verification = d.verification as { chunk_id: string; finding: string; verified: boolean; verification: string }[] | undefined
  const findings = d.findings as { chunk_id: string; finding: string; citation?: string }[] | undefined
  const targets = d.targets as { change_id: string; why: string }[] | undefined
  const toolCalls = d.tool_calls as { tool: string; arguments: unknown; result_ids: string[]; error: string | null }[] | undefined
  return (
    <div className="gdetail" aria-live="polite">
      <header className="gdetail__head">
        <span className="gdetail__seq">step {event.seq}</span>
        <strong>{STAGE_LABEL[event.node] ?? event.node}</strong>
        <span className={`mode mode--${event.mode}`}>{MODE_LABEL[event.mode] ?? event.mode}</span>
        {event.change_id && (onPick
          ? <button type="button" className="chip" onClick={() => onPick(event.change_id!)}>{event.change_id}</button>
          : <code className="gdetail__cid">{event.change_id}</code>)}
        <span className="muted tiny">{fmtMs(event.duration_ms)}</span>
        {onShowInTrace && <button type="button" className="btn btn--tiny gdetail__jump" onClick={() => onShowInTrace(event.seq)}>Show in list</button>}
      </header>
      <p className="small">{event.summary}</p>
      {targets && targets.length > 0 && <ul className="gdetail__list">{targets.map((t) => <li key={t.change_id}><code>{t.change_id}</code> {t.why}</li>)}</ul>}
      {Array.isArray(d.query) && <p className="small"><strong>Query</strong> ({String(d.strategy)}, top {String(d.k)}): <code>{(d.query as string[]).join(' ')}</code></p>}
      {hits && hits.length > 0 && <ul className="gdetail__list">{hits.map((h) => <li key={h.id}><code>{h.id}</code> {h.citation} <span className="muted">score {h.score}</span></li>)}</ul>}
      {toolCalls && toolCalls.length > 0 && (
        <div className="gdetail__tools">
          <p className="small"><strong>Tool calls made by the model ({toolCalls.length})</strong></p>
          <ul className="gdetail__list">{toolCalls.map((t, i) => (
            <li key={i}><code>{t.tool}</code> <code className="muted">{typeof t.arguments === 'string' ? t.arguments : JSON.stringify(t.arguments)}</code>
              {t.error ? <span className="error"> rejected: {t.error}</span> : <> → {t.result_ids.join(', ') || 'no results'}</>}</li>
          ))}</ul>
        </div>
      )}
      {typeof d.rationale === 'string' && <p className="small"><strong>Rationale</strong> {d.rationale}</p>}
      {findings && findings.length > 0 && <ul className="gdetail__list">{findings.map((f, i) => <li key={i}><code>{f.chunk_id}</code> {FINDING_LABEL[f.finding] ?? f.finding} {f.citation && <span className="muted">({f.citation})</span>}</li>)}</ul>}
      {results && results.length > 0 && <ul className="gdetail__list">{results.map((r) => <li key={r.test_id}><code>{r.test_id}</code> <StatusBadge status={r.status} /> {r.summary}</li>)}</ul>}
      {verification && verification.length > 0 && <ul className="gdetail__list">{verification.map((v, i) => <li key={i}><code>{v.chunk_id}</code> {FINDING_LABEL[v.finding] ?? v.finding}: {v.verified ? <span className="ok">verified</span> : <span className="error">discarded</span>} <span className="muted">({v.verification})</span></li>)}</ul>}
      {event.node === 'reflect' && typeof d.decision === 'string' && <p className="small"><strong>Decision</strong> {DECISION_LABEL[d.decision] ?? d.decision}{typeof d.reason === 'string' ? `: ${d.reason}` : ''}</p>}
      {event.node === 'decide' && Array.isArray(d.adjustments) && <ul className="gdetail__list">{(d.adjustments as { code: string; change_ids: string[] }[]).map((a, i) => <li key={i}>{a.code}: {a.change_ids.join(', ')}</li>)}</ul>}
      <details><summary className="tiny muted">Raw event</summary><pre className="json">{JSON.stringify(event.detail, null, 2)}</pre></details>
    </div>
  )
}

/* ================================================================ state graph view */

function StateGraphInner({ trace, onShowInTrace, onPick }: {
  trace: TraceEvent[]; onShowInTrace?: (seq: number) => void; onPick?: (id: string) => void
}) {
  const ordered = useMemo(() => [...trace].sort((a, b) => a.seq - b.seq), [trace])
  const ids = useMemo(() => traceChangeIds(ordered), [ordered])
  const [changeId, setChangeId] = useState('')
  const [selected, setSelected] = useState<GraphNodeId | null>(null)
  const [openSeq, setOpenSeq] = useState<number | null>(null)
  const [step, setStep] = useState<number | null>(null)   // replay position (index into ordered), null = whole run
  const [playing, setPlaying] = useState(false)
  const { fitView } = useReactFlow()
  const wrap = useRef<HTMLDivElement>(null)

  const visible = useMemo(() => (step === null ? ordered : ordered.slice(0, step + 1)), [ordered, step])
  const model = useMemo(() => buildStateGraph(visible, { changeId: changeId || undefined }), [visible, changeId])
  const replay = useMemo(() => (step === null ? null : replayAt(ordered, step)), [ordered, step])

  useEffect(() => {
    if (!playing) return
    const t = setInterval(() => setStep((s) => {
      const n = (s ?? -1) + 1
      if (n >= ordered.length - 1) { setPlaying(false); return ordered.length - 1 }
      return n
    }), 420)
    return () => clearInterval(t)
  }, [playing, ordered.length])

  useEffect(() => {
    const el = wrap.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => fitView({ padding: 0.06, minZoom: 0.62 }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [fitView])

  const nodes: Node[] = useMemo(() => [...(Object.keys(POS) as GraphNodeId[]).map((id): StageNode => ({
    id, type: 'stage', position: POS[id], draggable: false, selectable: id !== 'START' && id !== 'END',
    selected: selected === id,
    data: { id, stats: model.nodes[id], active: replay?.active === id, dim: model.nodes[id].visits === 0 && id !== 'START' },
  })), { id: 'pad', type: 'spacer', position: { x: COL.ring1, y: ROW.ring1 + H + BOTTOM_OFFSET + 30 }, selectable: false, draggable: false, data: {} }],
  [model, replay, selected])

  const edges: FlowEdge[] = useMemo(() => model.edges.map((st) => {
    const [s, t, offset] = ROUTE[st.spec.id]
    return {
      id: st.spec.id, source: st.spec.source, target: st.spec.target, sourceHandle: `s-${s}`, targetHandle: `t-${t}`,
      type: 'flow', selectable: false, zIndex: st.spec.id === 'reflect>retrieve' ? 1 : 0,
      data: { stats: st, walked: replay?.walked.has(st.spec.id) ?? false, replaying: !!replay, offset,
        sourceVisits: model.nodes[st.spec.source].visits },
    }
  }), [model, replay])

  const onNodeClick = useCallback((_: unknown, n: Node) => {
    const id = n.id as GraphNodeId
    if (id === 'START' || id === 'END' || n.id === 'pad') return
    setSelected((cur) => (cur === id ? null : id)); setOpenSeq(null)
  }, [])

  const sel = selected ? model.nodes[selected] : null
  const selEvents = sel ? ordered.filter((e) => sel.seqs.includes(e.seq)) : []
  const openEvent = openSeq !== null ? ordered.find((e) => e.seq === openSeq) : (replay?.event ?? undefined)
  const retries = model.edges.find((e) => e.spec.id === 'reflect>retrieve')?.decisions.retry ?? 0

  return (
    <div className="sg">
      <div className="sg-bar">
        <div className="sg-replay" role="group" aria-label="Replay the run step by step">
          <button type="button" className="btn btn--tiny" onClick={() => { setPlaying(false); setStep((s) => Math.max(0, (s ?? ordered.length - 1) - 1)) }}
            aria-label="Previous step">◀</button>
          <button type="button" className="btn btn--tiny sg-replay__play" onClick={() => {
            if (playing) { setPlaying(false); return }
            setStep((s) => (s === null || s >= ordered.length - 1 ? 0 : s)); setPlaying(true)
          }}>{playing ? 'Pause' : step === null ? 'Replay run' : 'Play'}</button>
          <button type="button" className="btn btn--tiny" onClick={() => { setPlaying(false); setStep((s) => Math.min(ordered.length - 1, (s ?? -1) + 1)) }}
            aria-label="Next step">▶</button>
          <input type="range" min={0} max={Math.max(0, ordered.length - 1)} value={step ?? ordered.length - 1}
            onChange={(e) => { setPlaying(false); setStep(Number(e.target.value)) }} aria-label="Replay position" />
          <span className="tiny muted sg-replay__pos">{step === null ? `${ordered.length} steps` : `step ${step + 1}/${ordered.length}`}</span>
          {step !== null && <button type="button" className="btn btn--tiny" onClick={() => { setPlaying(false); setStep(null) }}>Whole run</button>}
        </div>
        {ids.length > 1 && (
          <label className="select small">Change
            <select value={changeId} onChange={(e) => setChangeId(e.target.value)}>
              <option value="">All changes</option>
              {ids.map((id) => <option key={id} value={id}>{id}</option>)}
            </select>
          </label>
        )}
      </div>

      <div className="sg-canvas" ref={wrap}>
        <Markers />
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
          onNodeClick={onNodeClick} onPaneClick={() => setSelected(null)}
          fitView fitViewOptions={{ padding: 0.06, minZoom: 0.62 }} minZoom={0.4} maxZoom={1.6}
          nodesDraggable={false} nodesConnectable={false} elementsSelectable
          zoomOnScroll={false} preventScrolling={false} zoomOnDoubleClick={false}
          proOptions={{ hideAttribution: true }}>
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>
      </div>

      <div className="sg-legend" aria-label="Legend">
        <span><i className="sg-key sg-key--tool" /> tool step</span>
        <span><i className="sg-key sg-key--reason" /> reasoning step (rules or LLM)</span>
        <span><i className="sg-key sg-key--control" /> control / routing</span>
        <span><i className="sg-line" /> edge taken (thicker = more often)</span>
        <span><i className="sg-line sg-line--cond" /> conditional edge</span>
        {retries > 0 && <span><i className="sg-line sg-line--retry" /> bounded retry</span>}
        <span className="muted">Topology from <code>graph.py</code>; every count comes from this run's trace. Click a node for its visits; drag to pan.</span>
      </div>
      {model.unexpected.length > 0 && (
        <p className="tiny error">Transitions not in the compiled graph: {model.unexpected.map((u) => `${u.from}→${u.to} ×${u.count}`).join(', ')}</p>
      )}

      {replay?.event && openSeq === null && !sel && <EventDetail event={replay.event} onShowInTrace={onShowInTrace} onPick={onPick} />}
      {sel && selected && (
        <section className="sg-panel" aria-label={`${STAGE_LABEL[selected]} visits`}>
          <header className="sg-panel__head">
            <strong>{STAGE_LABEL[selected]}</strong>
            <span className="muted small">{STAGE_ROLE[selected]} · {sel.visits} visit{sel.visits === 1 ? '' : 's'} · {fmtMs(sel.totalMs)}</span>
            <button type="button" className="btn btn--tiny" onClick={() => setSelected(null)}>Close</button>
          </header>
          {selEvents.length === 0 ? <p className="muted small">This node did not run{changeId ? ` for ${changeId}` : ''}.</p> : (
            <div className="sg-visits">
              {selEvents.map((e) => (
                <button key={e.seq} type="button" className={`sg-visit sg-visit--${cellTone(e)}${openSeq === e.seq ? ' is-on' : ''}`}
                  onClick={() => setOpenSeq(openSeq === e.seq ? null : e.seq)} title={e.summary}>
                  <span className="sg-visit__seq">{e.seq}</span>
                  {e.change_id && <code>{e.change_id}</code>}
                  <span className={`sg-dot sg-dot--${e.mode}`} aria-label={MODE_LABEL[e.mode] ?? e.mode} />
                  <span className="sg-visit__res">{LOOP.includes(e.node as (typeof LOOP)[number]) ? cellResult(e) : e.summary}</span>
                </button>
              ))}
            </div>
          )}
          {openEvent && openSeq !== null && <EventDetail event={openEvent} onShowInTrace={onShowInTrace} onPick={onPick} />}
        </section>
      )}
    </div>
  )
}

export function ExecutionGraph({ trace, onShowInTrace, onPick }: {
  trace: TraceEvent[]; onShowInTrace?: (seq: number) => void; onPick?: (id: string) => void
}) {
  if (!trace.length) return <div className="ggraph__empty"><p className="muted">Run an investigation to see the execution graph.</p></div>
  return <ReactFlowProvider><StateGraphInner trace={trace} onShowInTrace={onShowInTrace} onPick={onPick} /></ReactFlowProvider>
}

/* ================================================================ runs view: one row per change attempt */

export function RunsTable({ trace, records, onPick, onShowInTrace }: {
  trace: TraceEvent[]; records: AgentRecord[]; onPick?: (id: string) => void; onShowInTrace?: (seq: number) => void
}) {
  const runs = useMemo(() => buildRuns(trace), [trace])
  const outcome = useMemo(() => new Map(records.map((r) => [r.change_id, r.outcome])), [records])
  const [open, setOpen] = useState<number | null>(null)
  const openEvent = open !== null ? trace.find((e) => e.seq === open) : undefined
  const cell = (e: TraceEvent | undefined) => e
    ? <button type="button" className={`rt-cell rt-cell--${cellTone(e)}${open === e.seq ? ' is-on' : ''}`} onClick={() => setOpen(open === e.seq ? null : e.seq)} title={e.summary}>
      <span className={`sg-dot sg-dot--${e.mode}`} aria-label={MODE_LABEL[e.mode] ?? e.mode} />{cellResult(e)}
    </button>
    : <span className="rt-cell rt-cell--none">–</span>
  return (
    <div className="rt">
      {runs.setup.length > 0 && (
        <p className="small rt-setup">
          {runs.setup.map((e) => <button key={e.seq} type="button" className="chip rt-setup__chip" onClick={() => setOpen(open === e.seq ? null : e.seq)}>{STAGE_LABEL[e.node]}</button>)}
          <span className="muted">{runs.setup.map((e) => e.summary).join(' · ')}</span>
        </p>
      )}
      <div className="tablewrap rt-wrap">
        <table className="table rt-table">
          <thead><tr><th>#</th><th>Change</th>{LOOP.map((s) => <th key={s} title={STAGE_ROLE[s]}>{STAGE_LABEL[s]}</th>)}<th>Outcome</th></tr></thead>
          <tbody>
            {runs.attempts.map((r, i) => {
              const o = outcome.get(r.changeId)
              const last = !runs.attempts.slice(i + 1).some((x) => x.changeId === r.changeId)
              return (
                <tr key={i} className={r.attempt > 1 ? 'rt-retry' : ''}>
                  <td className="tiny muted">{i + 1}</td>
                  <td className="nowrap">
                    {onPick ? <button type="button" className="chip" onClick={() => onPick(r.changeId)}>{r.changeId}</button> : <code>{r.changeId}</code>}
                    {r.attempt > 1 && <span className="rt-attempt" title="Second attempt after a bounded retry with a broadened query">↻ attempt {r.attempt}</span>}
                  </td>
                  {LOOP.map((s) => <td key={s}>{cell(r.cells[s])}</td>)}
                  <td>{last && o ? <span className={`tag tag--${OUTCOME_LABEL[o]?.tone ?? 'muted'}`}>{OUTCOME_LABEL[o]?.text ?? o}</span> : <span className="muted tiny">{last ? '—' : 'retried'}</span>}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {runs.final && <p className="small rt-setup"><button type="button" className="chip rt-setup__chip" onClick={() => setOpen(open === runs.final!.seq ? null : runs.final!.seq)}>Decide</button> <span className="muted">{runs.final.summary}</span></p>}
      {openEvent && <EventDetail event={openEvent} onShowInTrace={onShowInTrace} onPick={onPick} />}
    </div>
  )
}

