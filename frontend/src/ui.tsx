import type { ReactNode } from 'react'
import { DECISION_COPY, STATUS_LABEL } from './labels'
import type { Decision } from './types'

const ICONS: Record<string, string> = {
  compare: 'M4 6h7M4 12h7M4 18h7M15 6l5 6-5 6',
  history: 'M12 7v5l3 2M3.5 12a8.5 8.5 0 1 0 2.5-6M3 4v4h4',
  audit: 'M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6',
  chart: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  check: 'M5 12.5l4.5 4.5L19 7.5',
  alert: 'M12 4l9 16H3zM12 10v4M12 17.5v.5',
  play: 'M7 5l12 7-12 7z',
  refresh: 'M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4',
  swap: 'M7 7h13l-3-3M17 17H4l3 3',
  file: 'M7 3h7l5 5v13H7zM14 3v5h5',
  bolt: 'M13 2L4 14h7l-1 8 9-12h-7z',
}

export function Icon({ name, size = 18 }: { name: keyof typeof ICONS | string; size?: number }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={ICONS[name] ?? ''} />
    </svg>
  )
}

export function PageHeader({ title, subtitle, actions, eyebrow }: {
  title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; eyebrow?: ReactNode
}) {
  return (
    <header className="pagehead">
      <div className="pagehead__text">
        {eyebrow && <p className="pagehead__eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {subtitle && <p className="pagehead__sub">{subtitle}</p>}
      </div>
      {actions && <div className="pagehead__actions">{actions}</div>}
    </header>
  )
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <span className="spinner" style={{ width: size, height: size }} aria-hidden="true" />
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return <div className="state state--loading" role="status"><Spinner /> <span>{label}</span></div>
}

export function ErrorState({ message, onRetry, title = 'Something went wrong' }: {
  message: string; onRetry?: () => void; title?: string
}) {
  return (
    <div className="state state--error" role="alert">
      <Icon name="alert" />
      <div>
        <p className="state__title">{title}</p>
        <p className="state__msg">{message}</p>
      </div>
      {onRetry && <button type="button" className="btn btn--quiet" onClick={onRetry}><Icon name="refresh" size={15} /> Retry</button>}
    </div>
  )
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state--empty">
      <p className="state__title">{title}</p>
      {children && <div className="state__msg">{children}</div>}
    </div>
  )
}

export function StatusBadge({ status }: { status: string }) {
  const s = STATUS_LABEL[status] ?? { text: status, tone: 'muted' as const, hint: '' }
  return <span className={`tag tag--${s.tone}`} title={s.hint}>{s.text}</span>
}

export function DecisionWord({ d, big = false }: { d: Decision | null | undefined; big?: boolean }) {
  if (!d) return <span className="muted">none</span>
  return <span className={`word word--${d}${big ? ' word--big' : ''}`}>{DECISION_COPY[d].word}</span>
}

export function Tabs<T extends string>({ tabs, active, onChange, label }: {
  tabs: { id: T; label: string; badge?: ReactNode }[]; active: T; onChange: (t: T) => void; label: string
}) {
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" aria-selected={active === t.id} id={`tab-${t.id}`}
          className={`tabs__tab${active === t.id ? ' tabs__tab--on' : ''}`} onClick={() => onChange(t.id)}>
          {t.label}{t.badge !== undefined && <span className="tabs__badge">{t.badge}</span>}
        </button>
      ))}
    </div>
  )
}

export function Stat({ value, label, tone }: { value: ReactNode; label: string; tone?: 'bad' | 'warn' | 'ok' }) {
  return (
    <div className={`stat${tone ? ` stat--${tone}` : ''}`}>
      <span className="stat__v">{value}</span>
      <span className="stat__l">{label}</span>
    </div>
  )
}
