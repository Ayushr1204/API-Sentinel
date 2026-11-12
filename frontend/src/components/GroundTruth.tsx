import { SEVERITY_LABEL, ruleLabel } from '../labels'
import type { Analysis, GroundTruth, GroundTruthRow } from '../types'
import { DecisionWord, Empty, Stat } from '../ui'
import { SevMark } from './ResultsView'

const STATUS: Record<GroundTruthRow['status'], { text: string; tone: 'ok' | 'warn' | 'bad' }> = {
  matched: { text: 'Matched', tone: 'ok' },
  severity_mismatch: { text: 'Wrong severity', tone: 'warn' },
  missed: { text: 'Missed', tone: 'bad' },
}

/** One-line verdict used in the header: does this run agree with the sample's labels? */
export function GroundTruthBadge({ gt }: { gt: GroundTruth }) {
  const inv = gt.investigation
  const ok = gt.pass && (!inv || inv.decision_match)
  return (
    <span className={`tag tag--${ok ? 'ok' : 'bad'}`}
      title={`${gt.counts.matched + gt.counts.severity_mismatch}/${gt.counts.expected} labelled changes found, ${gt.counts.unexpected} unexpected`}>
      {ok ? 'Matches ground truth' : 'Differs from ground truth'}
    </span>
  )
}

export function GroundTruthPanel({ a, onPick }: { a: Analysis; onPick: (id: string) => void }) {
  const gt = a.ground_truth
  if (!gt) {
    return (
      <Empty title="No ground truth for this comparison">
        Ground truth exists for the bundled samples only: each sample folder ships an <code>expected.json</code> written
        by hand. Uploaded specs are not labelled.
      </Empty>
    )
  }
  const k = gt.counts
  const found = k.matched + k.severity_mismatch
  const inv = gt.investigation
  return (
    <div className="gt">
      <p className="muted small gt__intro">
        Sample <code>{a.sample}</code> ships a hand-written label: the changes a reviewer expects, how severe each one
        is, and the decision they would make. This compares the engine's output with it. Nothing is hidden: missed,
        unexpected and wrongly graded changes are all listed.
      </p>

      <div className="gt__decisions">
        <div className={`gt__decision gt__decision--${gt.decision_match ? 'ok' : 'bad'}`}>
          <p className="gt__label">Baseline decision</p>
          <p>Expected <DecisionWord d={gt.expected_decision} /> · got <DecisionWord d={gt.actual_decision} /></p>
          <p className="small">{gt.decision_match ? 'Matches.' : 'Does not match.'}</p>
        </div>
        {gt.decision_after_investigation && (
          <div className={`gt__decision gt__decision--${!inv ? 'muted' : inv.decision_match ? 'ok' : 'bad'}`}>
            <p className="gt__label">After investigation</p>
            <p>Expected <DecisionWord d={gt.decision_after_investigation} />
              {inv ? <> · got <DecisionWord d={inv.actual_decision} /></> : <span className="muted"> · not run yet</span>}</p>
            <p className="small">{inv ? (inv.decision_match ? 'Matches.' : 'Does not match.') + ' ' + inv.note
              : 'Run the investigation with the sample consumer files to check it.'}</p>
          </div>
        )}
      </div>
      {gt.rationale && <p className="small gt__why"><strong>Reviewer's reasoning:</strong> {gt.rationale}</p>}

      <div className="stats">
        <Stat value={<>{found}<small> / {k.expected}</small></>} label="labelled changes found" tone={k.missed ? 'bad' : 'ok'} />
        <Stat value={k.matched} label="severity correct" tone={k.severity_mismatch ? 'warn' : 'ok'} />
        <Stat value={k.missed} label="missed" tone={k.missed ? 'bad' : undefined} />
        <Stat value={k.unexpected} label="unexpected" tone={k.unexpected ? 'warn' : undefined} />
        <Stat value={`${Math.round(gt.precision * 100)}%`} label="precision" />
        <Stat value={`${Math.round(gt.recall * 100)}%`} label="recall" />
      </div>

      <div className="tablewrap">
        <table className="table">
          <thead>
            <tr><th>Expected change</th><th>Where</th><th>Expected severity</th><th>Predicted</th><th>Result</th></tr>
          </thead>
          <tbody>
            {gt.changes.map((r, i) => {
              const s = STATUS[r.status]
              return (
                <tr key={i}>
                  <td>
                    <strong>{ruleLabel(r.expected.rule)}</strong>
                    {r.expected.why && <span className="muted small gt__rowwhy">{r.expected.why}</span>}
                  </td>
                  <td className="small">
                    {r.expected.operation ? <code>{r.expected.operation}</code> : <span className="muted">document</span>}
                    {r.expected.field && <> · <code>{r.expected.field}</code></>}
                    {r.expected.direction && <span className="muted"> · {r.expected.direction}</span>}
                  </td>
                  <td className="small"><SevMark severity={r.expected.severity} /> {SEVERITY_LABEL[r.expected.severity]}</td>
                  <td className="small">
                    {r.change_id
                      ? <><button type="button" className="chip" onClick={() => onPick(r.change_id!)}>{r.change_id}</button>{' '}
                        {r.actual_severity && <>{SEVERITY_LABEL[r.actual_severity]}</>}</>
                      : <span className="muted">not detected</span>}
                  </td>
                  <td><span className={`tag tag--${s.tone}`}>{s.text}</span></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <h3 className="section__h gt__h">Detected but not in the label ({gt.unexpected.length})</h3>
      {gt.unexpected.length === 0
        ? <p className="muted small">None: every detected change was expected.</p>
        : (
          <ul className="gt__extra">
            {gt.unexpected.map((u) => (
              <li key={u.change_id} className="small">
                <button type="button" className="chip" onClick={() => onPick(u.change_id)}>{u.change_id}</button>{' '}
                <SevMark severity={u.severity} /> {ruleLabel(u.rule)} · <code>{u.location}</code>
              </li>
            ))}
          </ul>
        )}
    </div>
  )
}
