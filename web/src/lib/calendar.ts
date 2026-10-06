import type { CalendarItem } from '../api/types'
import type { BrandBundle } from '../state/useWorkspace'

export function calendarFor(bundle: BrandBundle): CalendarItem[] {
  const newsletters: CalendarItem[] = bundle.newsletters.map((issue) => ({
    item_type: 'newsletter',
    id: issue.id,
    external_source_id: null,
    title: issue.content?.final_title || issue.content?.working_title || 'Untitled newsletter',
    channel: 'beehiiv',
    status: issue.lifecycle,
    scheduled_for: issue.scheduled_for,
    url: issue.beehiiv_preview_url,
    body_summary: issue.content?.editorial_thesis || issue.content?.preview_text || null,
  }))
  return [...bundle.calendar, ...newsletters]
}
