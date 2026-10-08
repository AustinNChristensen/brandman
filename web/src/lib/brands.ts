import type { Brand } from '../api/types'

const PALETTE = ['#3F6B3A', '#B23A32', '#2F5FA8', '#6B4FBB', '#B5690B', '#0E7C7B', '#8A3B2E', '#3B5BDB']
const KNOWN: Record<string, string> = {
  'demo-brand': '#B23A32',
  'demo-personal': '#2F5FA8',
}

export function brandColor(slug: string): string {
  if (KNOWN[slug]) return KNOWN[slug]
  let h = 0
  for (const ch of slug) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return PALETTE[h % PALETTE.length]
}

export function brandInitials(brand: Pick<Brand, 'name'>): string {
  return brand.name.split(/\s+/).map((w) => w[0]).join('').slice(0, 3).toUpperCase()
}
