export function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

export function relTime(value: string | null | undefined, now = Date.now()): string {
  const d = parseDate(value)
  if (!d) return '—'
  const diff = Math.round((now - d.getTime()) / 1000)
  const abs = Math.abs(diff)
  const suffix = diff >= 0 ? ' ago' : ' from now'
  if (abs < 60) return 'just now'
  if (abs < 3600) return `${Math.floor(abs / 60)}m${suffix}`
  if (abs < 86400) return `${Math.floor(abs / 3600)}h${suffix}`
  if (abs < 86400 * 14) return `${Math.floor(abs / 86400)}d${suffix}`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function shortDateTime(value: string | null | undefined): string {
  const d = parseDate(value)
  if (!d) return '—'
  return d.toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

export function shortTime(value: string | null | undefined): string {
  const d = parseDate(value)
  if (!d) return ''
  return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
}

export function money(value: string | number | null | undefined, currency = 'USD'): string {
  if (value === null || value === undefined || value === '') return '—'
  const n = typeof value === 'number' ? value : Number(value)
  if (Number.isNaN(n)) return '—'
  return new Intl.NumberFormat(undefined, { style: 'currency', currency, maximumFractionDigits: 2 }).format(n)
}

export function compact(n: number): string {
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n)
}

export function titleCase(value: string): string {
  return value.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** Monday 00:00 local of the week containing `d`. */
export function startOfWeek(d: Date): Date {
  const out = new Date(d)
  out.setHours(0, 0, 0, 0)
  const day = (out.getDay() + 6) % 7
  out.setDate(out.getDate() - day)
  return out
}

export function addDays(d: Date, n: number): Date {
  const out = new Date(d)
  out.setDate(out.getDate() + n)
  return out
}

export function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}
