import { useMemo, type ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { useBrands } from '../state/BrandContext'
import { brandColor } from '../lib/brands'
import { Icon, type IconName } from './icons'

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: '/', label: 'Overview', icon: 'home' },
  { to: '/approvals', label: 'Approvals', icon: 'checkcircle' },
  { to: '/execution', label: 'Execution', icon: 'send' },
  { to: '/planner', label: 'Planner', icon: 'calendar' },
  { to: '/campaigns', label: 'Campaigns', icon: 'flag' },
  { to: '/content', label: 'Content', icon: 'file' },
  { to: '/sources', label: 'Sources', icon: 'rss' },
  { to: '/guidelines', label: 'Guidelines', icon: 'book' },
  { to: '/performance', label: 'Performance', icon: 'chart' },
  { to: '/learnings', label: 'Learnings', icon: 'bulb' },
  { to: '/feedback', label: 'Product feedback', icon: 'alert' },
  { to: '/engagement', label: 'Engagement', icon: 'mail' },
  { to: '/agents', label: 'Agents', icon: 'bot' },
  { to: '/integrations', label: 'Integrations', icon: 'plug' },
  { to: '/settings', label: 'Settings', icon: 'gear' },
]

export interface ShellMeters {
  approvalsWaiting?: number
  xRequests?: string
  spend?: string
}

export function Shell({ title, crumb, right, meters, children }: { title: ReactNode; crumb?: ReactNode; right?: ReactNode; meters?: ShellMeters; children: ReactNode }) {
  const { brands, selected, select } = useBrands()
  const location = useLocation()
  const search = location.search
  const dots = useMemo(() => (selected ? brands.filter((b) => b.slug === selected) : brands).slice(0, 4), [brands, selected])
  return (
    <div className="shell">
      <aside className="side">
        <div className="logo"><div className="logo-mark"><Icon name="layers" size={15} strokeWidth={2} /></div><div className="logo-name">Brand OS</div></div>
        <label className="brand-switch">
          <span style={{ display: 'flex', gap: 2 }}>{dots.map((b) => <span key={b.id} className="dot" style={{ background: brandColor(b.slug) }} />)}</span>
          <span>{selected ? brands.find((b) => b.slug === selected)?.name : 'All brands'}</span>
          <span style={{ marginLeft: 'auto', color: 'var(--faint)', display: 'flex' }}><Icon name="chevd" size={16} /></span>
          <select value={selected ?? ''} onChange={(e) => select(e.target.value || null)} aria-label="Brand">
            <option value="">All brands</option>
            {brands.map((b) => <option key={b.id} value={b.slug}>{b.name}</option>)}
          </select>
        </label>
        <nav className="nav">
          {NAV.map((item) => (
            <NavLink key={item.to} to={{ pathname: item.to, search }} end={item.to === '/'} className={({ isActive }) => (isActive ? 'on' : '')}>
              <Icon name={item.icon} />
              <span>{item.label}</span>
              {item.to === '/approvals' && meters?.approvalsWaiting ? <span className="badge">{meters.approvalsWaiting}</span> : null}
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          <div className="meter-row"><span>X requests (month)</span><span className="mono">{meters?.xRequests ?? '—'}</span></div>
          <div className="meter-row"><span>Estimated spend</span><span className="mono">{meters?.spend ?? '—'}</span></div>
          <div className="meta">Prices come only from your provider rate cards.</div>
        </div>
      </aside>
      <div className="main">
        <div className="mobile-bar">
          <label className="mobile-brand">
            <span>{selected ? brands.find((b) => b.slug === selected)?.name : 'All brands'}</span>
            <Icon name="chevd" size={15} />
            <select value={selected ?? ''} onChange={(e) => select(e.target.value || null)} aria-label="Brand">
              <option value="">All brands</option>
              {brands.map((b) => <option key={b.id} value={b.slug}>{b.name}</option>)}
            </select>
          </label>
          <nav className="mobile-nav" aria-label="Primary navigation">
            {NAV.map((item) => (
              <NavLink key={item.to} to={{ pathname: item.to, search }} end={item.to === '/'} className={({ isActive }) => (isActive ? 'on' : '')}>
                <Icon name={item.icon} size={15} />
                <span>{item.label}</span>
                {item.to === '/approvals' && meters?.approvalsWaiting ? <span className="badge">{meters.approvalsWaiting}</span> : null}
              </NavLink>
            ))}
          </nav>
        </div>
        <header className="top">
          {crumb && <span className="crumb">{crumb}</span>}
          <h1>{title}</h1>
          <span style={{ marginLeft: 'auto' }} />
          {right}
        </header>
        <div className="content">{children}</div>
      </div>
    </div>
  )
}
