import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ReactFlow,
  Controls,
  MiniMap,
  Handle,
  Position,
  useReactFlow,
  ReactFlowProvider,
  type NodeProps,
  type Node,
  type NodeTypes,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import type { TraceEvent } from '../../types'
import { MODE_LABEL, FINDING_LABEL } from '../../labels'
import { StatusBadge } from '../../ui'
import {
  traceToGraph,
  traceChangeIds,
  STAGE_LABEL,
  NODE_H,
  type GraphNodeData,
  type GraphFilter,
} from './traceGraph'

/* ================================================================
   Custom node component — defined at module level for stable ref
   ================================================================ */

type GNode = Node<GraphNodeData, 'graphNode'>

function GraphNode({ data, selected }: NodeProps<GNode>) {
  const d = data
  return (
    <div
      className={`gnode gnode--${d.mode}${selected ? ' gnode--sel' : ''}${d.isRetry ? ' gnode--retry' : ''}`}
      title={d.summary}
    >
      <Handle type="target" position={Position.Left} className="gnode__handle" />
      <div className="gnode__head">
        <span className={`gnode__dot gnode__dot--${d.mode}`} />
        <span className="gnode__label">{d.label}</span>
        <span className={`mode mode--${d.mode}`} style={{ fontSize: '0.56rem', padding: '0.1rem 0.2rem' }}>
          {MODE_LABEL[d.mode] ?? d.mode}
        </span>
      </div>
      {d.changeId && <code className="gnode__cid">{d.changeId}</code>}
      {d.reflectDecision && (
        <span className={`gnode__dec gnode__dec--${d.reflectDecision}`}>{d.reflectDecision}</span>
      )}
      <Handle type="source" position={Position.Right} className="gnode__handle" />
    </div>
  )
}

const nodeTypes: NodeTypes = { graphNode: GraphNode as React.ComponentType<NodeProps> }

/* ================================================================
   Detail panel — shows info for the selected event
   ================================================================ */

function NodeDetail({ event, onShowInTrace }: { event: TraceEvent; onShowInTrace?: (seq: number) => void }) {
  const d = event.detail as Record<string, unknown>
  const hits = d.hits as { id: string; citation: string; score: number }[] | undefined
  const results = d.results as { test_id: string; status: string; summary: string }[] | undefined
  const verification = d.verification as { chunk_id: string; finding: string; verified: boolean; verification: string }[] | undefined
  const findings = d.findings as { chunk_id: string; finding: string; citation?: string }[] | undefined
  const targets = d.targets as { change_id: string; why: string }[] | undefined
  const toolCalls = d.tool_calls as { tool: string; arguments: unknown; result_ids: string[]; error: string | null }[] | undefined

  return (
    <div className="gdetail">
      <header className="gdetail__head">
        <span className="gdetail__seq">#{event.seq}</span>
        <strong>{STAGE_LABEL[event.node] ?? event.node}</strong>
        <span className={`mode mode--${event.mode}`}>{MODE_LABEL[event.mode] ?? event.mode}</span>
        {event.change_id && <code className="gdetail__cid">{event.change_id}</code>}
        <span className="muted tiny">{event.duration_ms} ms</span>
        {onShowInTrace && (
          <button type="button" className="btn btn--tiny" style={{ marginLeft: 'auto' }}
            onClick={() => onShowInTrace(event.seq)}>
            Show in trace
          </button>
        )}
      </header>
      <p className="small">{event.summary}</p>

      {/* Plan targets */}
      {targets && targets.length > 0 && (
        <ul className="gdetail__list">{targets.map(t => (
          <li key={t.change_id}><code>{t.change_id}</code> {t.why}</li>
        ))}</ul>
      )}

      {/* Retrieve hits */}
      {Array.isArray(d.query) && (
        <p className="small"><strong>Query</strong> ({String(d.strategy)}, top {String(d.k)}): <code>{(d.query as string[]).join(' ')}</code></p>
      )}
      {hits && (
        <ul className="gdetail__list">{hits.map(h => (
          <li key={h.id}><code>{h.id}</code> {h.citation} <span className="muted">score {h.score}</span></li>
        ))}</ul>
      )}

      {/* Tool calls */}
      {toolCalls && toolCalls.length > 0 && (
        <div className="gdetail__tools">
          <p className="small"><strong>Tool calls ({toolCalls.length})</strong></p>
          <ul className="gdetail__list">{toolCalls.map((t, i) => (
            <li key={i}>
              <code>{t.tool}</code> <code className="muted">{typeof t.arguments === 'string' ? t.arguments : JSON.stringify(t.arguments)}</code>
              {t.error ? <span className="error"> rejected: {t.error}</span> : <> → {t.result_ids.join(', ') || 'no results'}</>}
            </li>
          ))}</ul>
        </div>
      )}

      {/* Assess findings */}
      {typeof d.rationale === 'string' && <p className="small"><strong>Rationale</strong> {d.rationale}</p>}
      {findings && findings.length > 0 && (
        <ul className="gdetail__list">{findings.map((f, i) => (
          <li key={i}><code>{f.chunk_id}</code> {FINDING_LABEL[f.finding] ?? f.finding} {f.citation && <span className="muted">({f.citation})</span>}</li>
        ))}</ul>
      )}

      {/* Execute results */}
      {results && (
        <ul className="gdetail__list">{results.map(r => (
          <li key={r.test_id}><code>{r.test_id}</code> <StatusBadge status={r.status} /> {r.summary}</li>
        ))}</ul>
      )}

      {/* Verify */}
      {verification && (
        <ul className="gdetail__list">{verification.map((v, i) => (
          <li key={i}><code>{v.chunk_id}</code> {FINDING_LABEL[v.finding] ?? v.finding}: {v.verified ? <span className="ok">verified</span> : <span className="error">discarded</span>} <span className="muted">({v.verification})</span></li>
        ))}</ul>
      )}

      {/* Reflect decision */}
      {typeof d.decision === 'string' && event.node === 'reflect' && (
        <p className="small"><strong>Decision</strong> {d.decision}{typeof d.reason === 'string' ? `: ${d.reason}` : ''}</p>
      )}

      {/* Baseline */}
      {event.node === 'baseline' && typeof d.decision === 'string' && (
        <p className="small"><strong>Baseline decision:</strong> {d.decision}</p>
      )}

      {/* Decide adjustments */}
      {event.node === 'decide' && Array.isArray(d.adjustments) && (
        <ul className="gdetail__list">{(d.adjustments as { code: string; change_ids: string[] }[]).map((a, i) => (
          <li key={i}>{a.code}: {a.change_ids.join(', ')}</li>
        ))}</ul>
      )}

      <details>
        <summary className="tiny muted">Raw event</summary>
        <pre className="json">{JSON.stringify(event.detail, null, 2)}</pre>
      </details>
    </div>
  )
}

/* ================================================================
   Legend
   ================================================================ */

function Legend() {
  return (
    <div className="glegend">
      <span className="glegend__title">Legend</span>
      <span className="glegend__item"><span className="gnode__dot gnode__dot--tool" /> Tool</span>
      <span className="glegend__item"><span className="gnode__dot gnode__dot--rules" /> Rules</span>
      <span className="glegend__item"><span className="gnode__dot gnode__dot--llm" /> LLM</span>
      <span className="glegend__sep" />
      <span className="glegend__item"><span className="glegend__line glegend__line--flow" /> Flow</span>
      <span className="glegend__item"><span className="glegend__line glegend__line--retry" /> Retry</span>
    </div>
  )
}

/* ================================================================
   Filter bar
   ================================================================ */

const FILTER_GROUPS: { label: string; nodes: string[] }[] = [
  { label: 'All', nodes: [] },
  { label: 'Planning & control', nodes: ['baseline', 'plan', 'reflect', 'decide'] },
  { label: 'Evidence & assessment', nodes: ['retrieve', 'assess', 'verify'] },
  { label: 'Testing', nodes: ['refine', 'execute'] },
]

function FilterBar({ trace, filter, onChange }: {
  trace: TraceEvent[]
  filter: GraphFilter
  onChange: (f: GraphFilter) => void
}) {
  const ids = useMemo(() => traceChangeIds(trace), [trace])
  const [group, setGroup] = useState(0)

  const pickGroup = (i: number) => {
    setGroup(i)
    const g = FILTER_GROUPS[i]
    onChange({ ...filter, nodeTypes: g.nodes.length ? new Set(g.nodes) : undefined })
  }
  const pickChange = (cid: string) => {
    onChange({ ...filter, changeId: cid || undefined })
  }

  return (
    <div className="gfilter">
      <div className="filters" role="group" aria-label="Filter graph nodes">
        {FILTER_GROUPS.map((g, i) => (
          <button key={g.label} type="button" className={`filter${group === i ? ' filter--on' : ''}`}
            onClick={() => pickGroup(i)}>{g.label}</button>
        ))}
      </div>
      {ids.length > 1 && (
        <label className="select small">Change
          <select value={filter.changeId ?? ''} onChange={e => pickChange(e.target.value)}>
            <option value="">All changes</option>
            {ids.map(id => <option key={id} value={id}>{id}</option>)}
          </select>
        </label>
      )}
    </div>
  )
}

/* ================================================================
   Inner graph (needs ReactFlowProvider ancestor)
   ================================================================ */

function GraphInner({ trace, onShowInTrace }: {
  trace: TraceEvent[]
  onShowInTrace?: (seq: number) => void
}) {
  const [filter, setFilter] = useState<GraphFilter>({})
  const [selectedSeq, setSelectedSeq] = useState<number | null>(null)
  const { fitView } = useReactFlow()
  const fitted = useRef(false)

  const { nodes, edges } = useMemo(() => traceToGraph(trace, filter), [trace, filter])

  // fitView once after initial render
  useEffect(() => {
    if (nodes.length && !fitted.current) {
      // small delay to let React Flow measure nodes
      const t = setTimeout(() => { fitView({ padding: 0.15 }); fitted.current = true }, 60)
      return () => clearTimeout(t)
    }
  }, [nodes.length, fitView])

  // Reset fit flag when filter changes
  useEffect(() => { fitted.current = false }, [filter])

  const selectedEvent = useMemo(
    () => (selectedSeq !== null ? trace.find(e => e.seq === selectedSeq) : undefined),
    [trace, selectedSeq],
  )

  const onNodeClick = useCallback((_ev: React.MouseEvent, node: Node) => {
    setSelectedSeq((node.data as GraphNodeData).event.seq)
  }, [])

  const onPaneClick = useCallback(() => setSelectedSeq(null), [])

  if (!nodes.length) {
    return (
      <div className="ggraph__empty">
        <p className="muted">No events match the current filters.</p>
      </div>
    )
  }

  // Compute height: number of rows × row spacing, min 300, max 650
  const maxY = Math.max(...nodes.map(n => n.position.y))
  const h = Math.min(650, Math.max(300, maxY + NODE_H + 80))

  return (
    <>
      <FilterBar trace={trace} filter={filter} onChange={setFilter} />
      <div className="ggraph__wrap" style={{ height: h }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={onNodeClick}
          onPaneClick={onPaneClick}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          minZoom={0.2}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
          selectNodesOnDrag={false}
        >
          <Controls showInteractive={false} />
          {nodes.length > 15 && (
            <MiniMap
              nodeColor={(n: Node) => {
                const m = (n.data as GraphNodeData | undefined)?.mode
                return m === 'tool' ? '#6ca8e4' : m === 'llm' ? '#c892e8' : '#5b6675'
              }}
              maskColor="rgba(0,0,0,0.6)"
              style={{ background: '#111820' }}
            />
          )}
        </ReactFlow>
      </div>
      <Legend />
      {selectedEvent && <NodeDetail event={selectedEvent} onShowInTrace={onShowInTrace} />}
    </>
  )
}

/* ================================================================
   Public component
   ================================================================ */

export function ExecutionGraph({ trace, onShowInTrace }: {
  trace: TraceEvent[]
  onShowInTrace?: (seq: number) => void
}) {
  if (!trace.length) {
    return (
      <div className="ggraph__empty">
        <p className="muted">Run an investigation to see the execution graph.</p>
      </div>
    )
  }
  return (
    <ReactFlowProvider>
      <GraphInner trace={trace} onShowInTrace={onShowInTrace} />
    </ReactFlowProvider>
  )
}

