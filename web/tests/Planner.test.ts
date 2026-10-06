import { describe, expect, it } from 'vitest'
import { calendarFor } from '../src/lib/calendar'
import type { BrandBundle } from '../src/state/useWorkspace'

describe('calendarFor', () => {
  it('includes newsletter issues alongside API calendar items', () => {
    const bundle = {
      calendar: [{
        item_type: 'post', id: 'post-1', external_source_id: null,
        title: 'X draft', channel: 'x', status: 'draft', scheduled_for: null,
        url: null, body_summary: 'Draft body',
      }],
      newsletters: [{
        id: 'issue-1', lifecycle: 'draft', scheduled_for: '2026-09-05T15:00:00Z',
        beehiiv_preview_url: 'https://app.beehiiv.com/posts/issue-1',
        content: {
          final_title: 'Madrid Is Calling', working_title: 'Madrid',
          editorial_thesis: 'Announce only with a plan.', preview_text: 'Do the math first.',
        },
      }],
    } as unknown as BrandBundle

    expect(calendarFor(bundle)).toEqual(expect.arrayContaining([
      expect.objectContaining({ item_type: 'post', id: 'post-1' }),
      expect.objectContaining({
        item_type: 'newsletter', id: 'issue-1', title: 'Madrid Is Calling',
        channel: 'beehiiv', scheduled_for: '2026-09-05T15:00:00Z',
      }),
    ]))
  })
})
