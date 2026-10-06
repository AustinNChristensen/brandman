import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { Brand } from '../api/types'
import { brandColor } from '../lib/brands'
import { Icon, type IconName } from './icons'

export function Card({ children, className = '', style }: { children: ReactNode; className?: string; style?: React.CSSProperties }) {
  return <div className={`card ${className}`} style={style}>{children}</div>
}

export function CardHeader({ icon, title, sub, right, iconColor }: { icon?: IconName; title: ReactNode; sub?: ReactNode; right?: ReactNode; iconColor?: string }) {
  return (
    <div className="card-h">
      {icon && <Icon name={icon} size={16} style={{ color: iconColor }} />}
      <span>{title}</span>
      {sub && <span className="sub">{sub}</span>}
      {right && <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>{right}</span>}
    </div>
  )
}

export function Chip({ kind = 'neutral', icon, children }: { kind?: 'agent' | 'human' | 'ok' | 'bad' | 'info' | 'neutral' | 'violet'; icon?: IconName; children: ReactNode }) {
  return <span className={`chip ${kind}`}>{icon && <Icon name={icon} size={12} />}{children}</span>
}

export function BrandTag({ brand, short = false }: { brand: Brand | undefined; short?: boolean }) {
  if (!brand) return <span className="brand"><span className="dot" style={{ background: 'var(--faint)' }} />Unknown</span>
  const label = short ? brand.name.split(/\s+/).map((w) => w[0]).join('').slice(0, 3).toUpperCase() : brand.name
  return <span className="brand" title={brand.name}><span className="dot" style={{ background: brandColor(brand.slug) }} />{label}</span>
}

export function Kpi({ label, value, sub, color, to }: { label: string; value: ReactNode; sub?: ReactNode; color?: string; to?: string }) {
  const body = (
    <div className="card kpi">
      <div className="lbl">{label}</div>
      <div className="val" style={{ color }}>{value}</div>
      {sub && <div className="meta">{sub}</div>}
    </div>
  )
  return to ? <Link to={to} style={{ color: 'inherit' }}>{body}</Link> : body
}

export function Bar({ pct, color }: { pct: number; color?: string }) {
  const width = `${Math.max(0, Math.min(100, pct))}%`
  return <div className="bar"><div style={{ width, background: color }} /></div>
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return <div className="state loading">{label}</div>
}
export function ErrorState({ message, retry }: { message: string; retry?: () => void }) {
  return (
    <div className="state error">
      <div>{message}</div>
      {retry && <button className="btn sm" style={{ marginTop: 10 }} onClick={retry}><Icon name="refresh" size={14} />Retry</button>}
    </div>
  )
}
export function Empty({ children }: { children: ReactNode }) {
  return <div className="state empty">{children}</div>
}

export function StatusChip({ status }: { status: string }) {
  const s = status.toLowerCase()
  const kind = s === 'published' || s === 'measured' || s === 'approved' || s === 'accepted' ? 'ok'
    : s === 'scheduled' || s === 'queued' || s === 'exported' || s === 'testing' ? 'info'
    : s === 'awaiting_approval' || s === 'fact_checked' || s === 'proposed' ? 'human'
    : s === 'rejected' || s === 'cancelled' || s === 'abandoned' ? 'bad'
    : 'neutral'
  return <Chip kind={kind}>{s.replace(/_/g, ' ')}</Chip>
}

export function Sparkline({ points, width = 120, height = 32, color = 'var(--accent)' }: { points: number[]; width?: number; height?: number; color?: string }) {
  if (points.length < 2) return null
  const max = Math.max(...points), min = Math.min(...points)
  const range = max - min || 1
  const step = width / (points.length - 1)
  const d = points.map((p, i) => `${(i * step).toFixed(1)},${(height - ((p - min) / range) * (height - 4) - 2).toFixed(1)}`).join(' ')
  return <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}><polyline fill="none" stroke={color} strokeWidth={1.75} strokeLinejoin="round" points={d} /></svg>
}

export function Modal({ title, children, footer, onClose }: { title: ReactNode; children: ReactNode; footer?: ReactNode; onClose: () => void }) {
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal" role="dialog" aria-modal="true">
        <div className="modal-h">{title}</div>
        <div className="modal-b">{children}</div>
        {footer && <div className="modal-f">{footer}</div>}
      </div>
    </div>
  )
}

export function KV({ rows }: { rows: { k: string; v: ReactNode }[] }) {
  return (
    <div className="kv">
      {rows.map((r) => <FragmentRow key={r.k} k={r.k} v={r.v} />)}
    </div>
  )
}
function FragmentRow({ k, v }: { k: string; v: ReactNode }) {
  return <><span className="k">{k}</span><span style={{ overflowWrap: 'anywhere' }}>{v}</span></>
}
