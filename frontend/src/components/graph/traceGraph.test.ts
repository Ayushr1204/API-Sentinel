/**
 * Unit tests for traceGraph.ts — event-to-node/edge mapping, filtering,
 * and missing-field handling.
 *
 * Run with:  npx vitest run src/components/graph/traceGraph.test.ts
 * Or:        npx tsx src/components/graph/traceGraph.test.ts
 */

import { traceToGraph, traceChangeIds, countRetries, type GraphFilter } from './traceGraph'
import type { TraceEvent } from '../../types'

/* ---------------------------------------------------------------- helpers */

function ev(seq: number, node: string, mode: 'tool' | 'rules' | 'llm' = 'rules', change_id: string | null = null, detail: Record<string, unknown> = {}): TraceEvent {
  return { seq, node, mode, change_id, summary: `${node} step`, detail, started_at: new Date().toISOString(), duration_ms: 10 }
}

/** A minimal 3-change trace with 1 retry on C002. */
function sampleTrace(): TraceEvent[] {
  return [
    ev(1, 'baseline', 'tool'),
    ev(2, 'plan', 'rules', null, { targets: [{ change_id: 'C001', why: 'test' }, { change_id: 'C002', why: 'test' }] }),
    // -- C001 --
    ev(3, 'retrieve', 'tool', 'C001', { query: ['field'], strategy: 'focused', k: 5, hits: [] }),
    ev(4, 'assess', 'rules', 'C001'),
    ev(5, 'refine', 'rules', 'C001'),
    ev(6, 'execute', 'tool', 'C001'),
    ev(7, 'verify', 'rules', 'C001'),
    ev(8, 'reflect', 'rules', 'C001', { decision: 'next' }),
    // -- C002 attempt 1 --
    ev(9, 'retrieve', 'tool', 'C002', { query: ['url'], strategy: 'focused', k: 5, hits: [] }),
    ev(10, 'assess', 'rules', 'C002'),
    ev(11, 'refine', 'rules', 'C002'),
    ev(12, 'execute', 'tool', 'C002'),
    ev(13, 'verify', 'rules', 'C002'),
    ev(14, 'reflect', 'rules', 'C002', { decision: 'retry', reason: 'no verified findings' }),
    // -- C002 attempt 2 (retry) --
    ev(15, 'retrieve', 'tool', 'C002', { query: ['tracking', 'url'], strategy: 'broadened', k: 10, hits: [{ id: 'E001', citation: 'file.py:12', score: 4.2 }] }),
    ev(16, 'assess', 'llm', 'C002'),
    ev(17, 'refine', 'rules', 'C002'),
    ev(18, 'execute', 'tool', 'C002'),
    ev(19, 'verify', 'rules', 'C002'),
    ev(20, 'reflect', 'rules', 'C002', { decision: 'stop' }),
    // -- decide --
    ev(21, 'decide', 'rules'),
  ]
}

/* ---------------------------------------------------------------- tests */

let passed = 0
let failed = 0

function assert(condition: boolean, msg: string) {
  if (condition) { passed++; return }
  failed++
  console.error(`  FAIL: ${msg}`)
}

function test(name: string, fn: () => void) {
  try { fn() } catch (e) { failed++; console.error(`  FAIL (threw): ${name}: ${e}`) }
}

// -- basic structure --
test('empty trace returns empty graph', () => {
  const { nodes, edges } = traceToGraph([])
  assert(nodes.length === 0, 'expected 0 nodes')
  assert(edges.length === 0, 'expected 0 edges')
})

test('all events become nodes', () => {
  const trace = sampleTrace()
  const { nodes } = traceToGraph(trace)
  assert(nodes.length === trace.length, `expected ${trace.length} nodes, got ${nodes.length}`)
})

test('edges connect consecutive events', () => {
  const trace = sampleTrace()
  const { edges } = traceToGraph(trace)
  assert(edges.length === trace.length - 1, `expected ${trace.length - 1} edges, got ${edges.length}`)
  // first edge: ev-1 → ev-2
  assert(edges[0].source === 'ev-1' && edges[0].target === 'ev-2', 'first edge connects seq 1→2')
})

// -- row assignment --
test('baseline and plan share row 0', () => {
  const { nodes } = traceToGraph(sampleTrace())
  const baseY = nodes.find(n => n.data.nodeType === 'baseline')!.position.y
  const planY = nodes.find(n => n.data.nodeType === 'plan')!.position.y
  assert(baseY === planY, 'baseline and plan should be on the same row')
  assert(baseY === 0, 'row 0 should have y=0')
})

test('each retrieve starts a new row', () => {
  const { nodes } = traceToGraph(sampleTrace())
  const retrieves = nodes.filter(n => n.data.nodeType === 'retrieve')
  assert(retrieves.length === 3, 'expected 3 retrieves')
  const ys = retrieves.map(n => n.position.y)
  assert(ys[0] < ys[1] && ys[1] < ys[2], 'retrieves should be on increasing rows')
})

test('events within a change share a row with their retrieve', () => {
  const { nodes } = traceToGraph(sampleTrace())
  // C001: retrieve(3), assess(4), ..., reflect(8) should all be on the same row
  const c1nodes = nodes.filter(n => n.data.event.seq >= 3 && n.data.event.seq <= 8)
  const c1ys = new Set(c1nodes.map(n => n.position.y))
  assert(c1ys.size === 1, 'all C001 events should share one row')
})

test('decide gets its own final row', () => {
  const { nodes } = traceToGraph(sampleTrace())
  const decideY = nodes.find(n => n.data.nodeType === 'decide')!.position.y
  const maxOtherY = Math.max(...nodes.filter(n => n.data.nodeType !== 'decide').map(n => n.position.y))
  assert(decideY > maxOtherY, 'decide should be below all other events')
})

// -- column assignment --
test('node X position corresponds to stage column', () => {
  const { nodes } = traceToGraph(sampleTrace())
  const baseline = nodes.find(n => n.data.nodeType === 'baseline')!
  const plan = nodes.find(n => n.data.nodeType === 'plan')!
  assert(baseline.position.x < plan.position.x, 'baseline column < plan column')
  const retrieve = nodes.find(n => n.data.nodeType === 'retrieve')!
  const assess = nodes.find(n => n.data.nodeType === 'assess')!
  assert(retrieve.position.x < assess.position.x, 'retrieve column < assess column')
})

// -- retry detection --
test('retry retrieve is marked isRetry', () => {
  const { nodes } = traceToGraph(sampleTrace())
  // seq 15 is the retry retrieve for C002
  const retryNode = nodes.find(n => n.data.event.seq === 15)!
  assert(retryNode.data.isRetry === true, 'retry retrieve should have isRetry=true')
  // seq 9 (first attempt) should NOT be a retry
  const firstAttempt = nodes.find(n => n.data.event.seq === 9)!
  assert(firstAttempt.data.isRetry === false, 'first attempt should not be isRetry')
})

test('retry edge is animated and styled', () => {
  const { edges } = traceToGraph(sampleTrace())
  // reflect(14) → retrieve(15) is the retry edge
  const retryEdge = edges.find(e => e.source === 'ev-14' && e.target === 'ev-15')!
  assert(retryEdge.animated === true, 'retry edge should be animated')
  assert(retryEdge.data.kind === 'retry', 'retry edge kind should be "retry"')
  assert(retryEdge.label === 'retry', 'retry edge should have label "retry"')
  assert(retryEdge.style.stroke === '#e0a050', 'retry edge should be amber')
})

test('normal flow edges are not animated', () => {
  const { edges } = traceToGraph(sampleTrace())
  const flowEdge = edges.find(e => e.source === 'ev-3' && e.target === 'ev-4')!
  assert(flowEdge.animated === false, 'flow edge should not be animated')
  assert(flowEdge.data.kind === 'flow', 'flow edge kind should be "flow"')
})

test('next-change edge is classified correctly', () => {
  const { edges } = traceToGraph(sampleTrace())
  // reflect(8,C001) → retrieve(9,C002)
  const nextEdge = edges.find(e => e.source === 'ev-8' && e.target === 'ev-9')!
  assert(nextEdge.data.kind === 'next_change', 'cross-change edge should be "next_change"')
})

test('to-decide edge is classified correctly', () => {
  const { edges } = traceToGraph(sampleTrace())
  // reflect(20) → decide(21)
  const toDecide = edges.find(e => e.target === 'ev-21')!
  assert(toDecide.data.kind === 'to_decide', 'edge to decide should be "to_decide"')
})

// -- reflect decision --
test('reflect nodes carry reflectDecision', () => {
  const { nodes } = traceToGraph(sampleTrace())
  const retryReflect = nodes.find(n => n.data.event.seq === 14)!
  assert(retryReflect.data.reflectDecision === 'retry', 'reflect(14) should have decision "retry"')
  const nextReflect = nodes.find(n => n.data.event.seq === 8)!
  assert(nextReflect.data.reflectDecision === 'next', 'reflect(8) should have decision "next"')
})

// -- filtering --
test('filter by nodeTypes', () => {
  const filter: GraphFilter = { nodeTypes: new Set(['baseline', 'plan', 'decide']) }
  const { nodes, edges } = traceToGraph(sampleTrace(), filter)
  assert(nodes.length === 3, `expected 3 nodes, got ${nodes.length}`)
  assert(edges.length === 2, `expected 2 edges, got ${edges.length}`)
})

test('filter by changeId keeps global + filtered change', () => {
  const filter: GraphFilter = { changeId: 'C001' }
  const { nodes } = traceToGraph(sampleTrace(), filter)
  const types = nodes.map(n => n.data.nodeType)
  assert(types.includes('baseline'), 'should include baseline')
  assert(types.includes('plan'), 'should include plan')
  assert(types.includes('decide'), 'should include decide')
  // Should only have C001's retrieve..reflect, not C002's
  const changeNodes = nodes.filter(n => n.data.changeId !== null)
  assert(changeNodes.every(n => n.data.changeId === 'C001'), 'all change nodes should be C001')
})

test('combined filter: nodeTypes + changeId', () => {
  const filter: GraphFilter = { nodeTypes: new Set(['retrieve', 'assess']), changeId: 'C001' }
  const { nodes } = traceToGraph(sampleTrace(), filter)
  assert(nodes.length === 2, `expected 2 nodes (retrieve+assess for C001), got ${nodes.length}`)
})

test('filter that excludes everything returns empty', () => {
  const filter: GraphFilter = { changeId: 'NONEXISTENT' }
  const { nodes } = traceToGraph(sampleTrace(), filter)
  // Global nodes (no change_id) pass the filter, so baseline, plan, decide remain
  assert(nodes.length === 3, `expected 3 global nodes, got ${nodes.length}`)
})

// -- helpers --
test('traceChangeIds returns unique IDs in order', () => {
  const ids = traceChangeIds(sampleTrace())
  assert(ids.length === 2, `expected 2 change IDs, got ${ids.length}`)
  assert(ids[0] === 'C001' && ids[1] === 'C002', 'order should be C001, C002')
})

test('countRetries counts retry reflects', () => {
  const n = countRetries(sampleTrace())
  assert(n === 1, `expected 1 retry, got ${n}`)
})

// -- missing / malformed fields --
test('trace with missing detail fields does not crash', () => {
  const trace: TraceEvent[] = [
    ev(1, 'baseline', 'tool'),
    { seq: 2, node: 'plan', mode: 'rules', change_id: null, summary: '', detail: {} as Record<string, unknown>, started_at: '', duration_ms: 0 },
    { seq: 3, node: 'retrieve', mode: 'tool', change_id: 'C001', summary: '', detail: {} as Record<string, unknown>, started_at: '', duration_ms: 0 },
    { seq: 4, node: 'reflect', mode: 'rules', change_id: 'C001', summary: '', detail: {} as Record<string, unknown>, started_at: '', duration_ms: 0 },
    ev(5, 'decide', 'rules'),
  ]
  const { nodes, edges } = traceToGraph(trace)
  assert(nodes.length === 5, 'should handle missing fields')
  assert(edges.length === 4, 'edges should still connect')
})

test('single-event trace produces node but no edges', () => {
  const { nodes, edges } = traceToGraph([ev(1, 'baseline', 'tool')])
  assert(nodes.length === 1, 'one node')
  assert(edges.length === 0, 'no edges')
})

test('unsorted trace is sorted by seq', () => {
  const trace = [ev(3, 'decide', 'rules'), ev(1, 'baseline', 'tool'), ev(2, 'plan', 'rules')]
  const { nodes } = traceToGraph(trace)
  assert(nodes[0].data.event.seq === 1, 'first node should be seq 1')
  assert(nodes[2].data.event.seq === 3, 'last node should be seq 3')
})

// -- report --
console.log(`\nGraph unit tests: ${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)

