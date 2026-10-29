/**
 * traceGraph.ts — Convert an investigation trace into a directed graph layout.
 *
 * Pure module with no React dependency — testable without a DOM.
 *
 * Layout: horizontal pipeline (X = stage column, Y = investigation round).
 * Edges connect consecutive events in recorded execution order.
 * Retry edges (reflect → retrieve for the same change) are visually distinct.
 *
 * Limitation: the backend trace has no explicit parent–child metadata.
 * Connections are inferred from execution sequence, not causal relationships.
 */

import type { TraceEvent } from '../../types'

/* ---------------------------------------------------------------- types */

export interface GraphNodeData {
  event: TraceEvent
  label: string
  nodeType: string
  mode: string
  changeId: string | null
  summary: string
  isRetry: boolean
  reflectDecision?: string
  [key: string]: unknown          // required by React Flow's Record<string,unknown> constraint
}

export type EdgeKind = 'flow' | 'retry' | 'next_change' | 'to_decide'

export interface LayoutNode {
  id: string
  type: string                    // React Flow custom node type name
  position: { x: number; y: number }
  data: GraphNodeData
}

export interface LayoutEdge {
  id: string
  source: string
  target: string
  type: string                    // React Flow edge type
  animated: boolean
  style: { stroke: string; strokeWidth: number; strokeDasharray?: string }
  label?: string
  labelStyle?: { fontSize: number; fill: string }
  labelBgStyle?: { fill: string; fillOpacity?: number }
  data: { kind: EdgeKind; [key: string]: unknown }
}

export interface GraphFilter {
  nodeTypes?: Set<string>
  changeId?: string
}

/* ---------------------------------------------------------------- constants */

export const STAGES = [
  'baseline', 'plan', 'retrieve', 'assess', 'refine',
  'execute', 'verify', 'reflect', 'decide',
] as const
export type Stage = (typeof STAGES)[number]

export const STAGE_LABEL: Record<string, string> = {
  baseline: 'Baseline', plan: 'Plan', retrieve: 'Retrieve',
  assess: 'Assess', refine: 'Refine', execute: 'Execute',
  verify: 'Verify', reflect: 'Reflect', decide: 'Decide',
}

const STAGE_COL: Record<string, number> = {}
STAGES.forEach((s, i) => { STAGE_COL[s] = i })

export const NODE_W = 120
export const NODE_H = 46
export const COL_GAP = 30
export const ROW_GAP = 60

/* ---------------------------------------------------------------- layout helpers */

function xOf(stage: string): number {
  return (STAGE_COL[stage] ?? 0) * (NODE_W + COL_GAP)
}
function yOf(row: number): number {
  return row * (NODE_H + ROW_GAP)
}

/* ---------------------------------------------------------------- core */

export function traceToGraph(
  trace: TraceEvent[],
  filter?: GraphFilter,
): { nodes: LayoutNode[]; edges: LayoutEdge[] } {
  if (!trace.length) return { nodes: [], edges: [] }

  const sorted = [...trace].sort((a, b) => a.seq - b.seq)

  // Collect reflect decisions from the full trace (before filtering)
  const reflectDec = new Map<number, string>()
  for (const e of sorted) {
    if (e.node === 'reflect' && e.detail) {
      reflectDec.set(e.seq, String(e.detail.decision ?? ''))
    }
  }

  // Apply filters
  let events = sorted
  if (filter?.nodeTypes?.size) {
    events = events.filter(e => filter.nodeTypes!.has(e.node))
  }
  if (filter?.changeId) {
    events = events.filter(e => !e.change_id || e.change_id === filter.changeId)
  }
  if (!events.length) return { nodes: [], edges: [] }

  // Assign rows: row 0 = setup (baseline + plan)
  //              each `retrieve` starts a new row
  //              `decide` gets the final row
  let row = 0
  const seqRow = new Map<number, number>()
  const retrySeqs = new Set<number>()
  let lastReflect: string | null = null

  for (const e of events) {
    if (e.node === 'baseline' || e.node === 'plan') {
      seqRow.set(e.seq, 0)
    } else if (e.node === 'retrieve') {
      row++
      seqRow.set(e.seq, row)
      if (lastReflect === 'retry') retrySeqs.add(e.seq)
      lastReflect = null
    } else if (e.node === 'decide') {
      row++
      seqRow.set(e.seq, row)
    } else {
      seqRow.set(e.seq, row)
      if (e.node === 'reflect') lastReflect = reflectDec.get(e.seq) ?? null
    }
  }

  // Build nodes
  const nodes: LayoutNode[] = events.map(e => ({
    id: `ev-${e.seq}`,
    type: 'graphNode',
    position: { x: xOf(e.node), y: yOf(seqRow.get(e.seq) ?? 0) },
    data: {
      event: e,
      nodeType: e.node,
      label: STAGE_LABEL[e.node] ?? e.node,
      mode: e.mode,
      changeId: e.change_id,
      summary: e.summary,
      isRetry: retrySeqs.has(e.seq),
      reflectDecision: e.node === 'reflect' ? reflectDec.get(e.seq) : undefined,
    },
  }))

  // Build edges — connect consecutive (filtered) events
  const edges: LayoutEdge[] = []
  for (let i = 0; i < events.length - 1; i++) {
    const a = events[i], b = events[i + 1]
    const isRetry = a.node === 'reflect' && reflectDec.get(a.seq) === 'retry'
    const isNext = a.node === 'reflect' && b.node === 'retrieve' && a.change_id !== b.change_id
    const kind: EdgeKind = isRetry ? 'retry'
      : isNext ? 'next_change'
        : b.node === 'decide' ? 'to_decide'
          : 'flow'

    edges.push({
      id: `e-${a.seq}-${b.seq}`,
      source: `ev-${a.seq}`,
      target: `ev-${b.seq}`,
      type: 'smoothstep',
      animated: isRetry,
      style: {
        stroke: isRetry ? '#e0a050' : '#2e3b4d',
        strokeWidth: isRetry ? 2 : 1.5,
        ...(isRetry ? { strokeDasharray: '6 3' } : {}),
      },
      ...(isRetry ? {
        label: 'retry',
        labelStyle: { fontSize: 10, fill: '#e0a050' },
        labelBgStyle: { fill: '#151b24', fillOpacity: 0.9 },
      } : {}),
      data: { kind },
    })
  }

  return { nodes, edges }
}

/** Extract unique change IDs from a trace (in order of first appearance). */
export function traceChangeIds(trace: TraceEvent[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const e of trace) {
    if (e.change_id && !seen.has(e.change_id)) { seen.add(e.change_id); out.push(e.change_id) }
  }
  return out
}

/** Count retries (reflect events with decision === 'retry'). */
export function countRetries(trace: TraceEvent[]): number {
  return trace.filter(e => e.node === 'reflect' && (e.detail?.decision as string) === 'retry').length
}

