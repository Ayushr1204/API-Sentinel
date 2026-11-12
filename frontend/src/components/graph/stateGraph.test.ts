/**
 * Unit tests for stateGraph.ts (pure functions, no DOM).
 * Run:  node --experimental-strip-types src/components/graph/stateGraph.test.ts
 */
import type { TraceEvent } from '../../types'
import { EDGES, buildRuns, buildStateGraph, cellResult, replayAt, traceChangeIds } from './stateGraph.ts'

let passed = 0
let failed = 0
function check(ok: boolean, msg: string) { if (ok) passed++; else { failed++; console.error(`  FAIL: ${msg}`) } }

function ev(seq: number, node: string, mode: 'tool' | 'rules' | 'llm' = 'rules', change_id: string | null = null,
  detail: Record<string, unknown> = {}): TraceEvent {
  return { seq, node, mode, change_id, summary: `${node} step`, detail, started_at: '', duration_ms: 10 }
}
const loop = (start: number, cid: string, decision: string, assessMode: 'rules' | 'llm' = 'rules') => [
  ev(start, 'retrieve', 'tool', cid, { hits: [{ id: 'E1' }], strategy: 'focused' }),
  ev(start + 1, 'assess', assessMode, cid),
  ev(start + 2, 'refine', 'rules', cid, { tests: [] }),
  ev(start + 3, 'execute', 'tool', cid, { results: [{ status: 'failed' }, { status: 'passed' }] }),
  ev(start + 4, 'verify', 'rules', cid, { verification: [{ verified: true }] }),
  ev(start + 5, 'reflect', 'rules', cid, { decision }),
]
const trace: TraceEvent[] = [
  ev(1, 'baseline', 'tool'), ev(2, 'plan'),
  ...loop(3, 'C001', 'next'), ...loop(9, 'C002', 'retry'), ...loop(15, 'C002', 'replan', 'llm'), ...loop(21, 'C003', 'stop'),
  ev(27, 'decide'),
]

const g = buildStateGraph(trace)
const count = (id: string) => g.edges.find((e) => e.spec.id === id)!.count
check(g.nodes.retrieve.visits === 4, 'retrieve visited 4 times')
check(g.nodes.assess.modes.llm === 1 && g.nodes.assess.modes.rules === 3, 'assess modes split rules/llm')
check(count('START>baseline') === 1 && count('decide>END') === 1, 'start and end edges counted once')
check(count('plan>retrieve') === 1 && count('plan>decide') === 0, 'plan routed to retrieve, not decide')
check(count('reflect>retrieve') === 3 && count('reflect>decide') === 1, 'loop taken 3 times, exit once')
const loopEdge = g.edges.find((e) => e.spec.id === 'reflect>retrieve')!
check(loopEdge.decisions.next === 1 && loopEdge.decisions.retry === 1 && loopEdge.decisions.replan === 1, 'loop split by decision')
check(g.unexpected.length === 0, 'no transitions outside the compiled graph')
check(g.complete, 'run reached decide')
check(EDGES.length === 12, 'graph.py has 12 edges including START and END')

const only = buildStateGraph(trace, { changeId: 'C002' })
check(only.nodes.retrieve.visits === 2, 'filter: C002 retrieved twice')
check(only.edges.find((e) => e.spec.id === 'reflect>retrieve')!.decisions.retry === 1, 'filter: C002 retry counted')
check(only.nodes.START.visits === 0, 'filter: START not counted')

const odd = buildStateGraph([ev(1, 'baseline'), ev(2, 'verify'), ev(3, 'decide')])
check(odd.unexpected.some((u) => u.from === 'baseline' && u.to === 'verify'), 'unexpected transitions are reported')

const partial = buildStateGraph(trace.slice(0, 5))
check(!partial.complete && partial.nodes.END.visits === 0, 'unfinished run has no END')

const r = replayAt(trace, 7)  // index 7 = seq 8, the first reflect
check(r.active === 'reflect' && r.walked.has('verify>reflect') && !r.walked.has('reflect>decide'), 'replay walks edges up to the step')

const runs = buildRuns(trace)
check(runs.setup.length === 2 && runs.final?.node === 'decide', 'runs: setup and final split off')
check(runs.attempts.length === 4, 'runs: one row per attempt')
check(runs.attempts[2].changeId === 'C002' && runs.attempts[2].attempt === 2, 'runs: retry is attempt 2')
check(runs.attempts[1].decision === 'retry', 'runs: reflect decision kept')
check(cellResult(trace[2]) === '1 hit', 'cell: retrieve hits')
check(cellResult(trace[5]) === '1/2 failed', 'cell: execute failures')
check(cellResult(trace[6]) === '1/1 verified', 'cell: verify')
check(traceChangeIds(trace).join(',') === 'C001,C002,C003', 'change ids in order')
check(buildStateGraph([]).nodes.START.visits === 0, 'empty trace is safe')
check(buildRuns([ev(1, 'reflect', 'rules', 'C9', {})]).attempts.length === 1, 'missing detail is safe')

console.log(`stateGraph tests: ${passed} passed, ${failed} failed`)
if (failed) (globalThis as { process?: { exit(n: number): void } }).process?.exit(1)
