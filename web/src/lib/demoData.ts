/** Sample brands shipped for a click-through, not a customer's measurements. */

const SAMPLE_SLUGS = new Set(['demo-brand', 'demo-personal', 'demo-fictional-studio'])

export function isSampleBrand(brand: { slug: string; name: string }): boolean {
  return SAMPLE_SLUGS.has(brand.slug) || brand.name.startsWith('[DEMO]')
}
