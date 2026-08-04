import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import EntryList from '../EntryList.vue'
import type { Entry } from '@/api/types'

/** One entry, with artwork unless `image` is overridden away. */
function entry(overrides: Partial<Entry['work']> = {}): Entry {
  return {
    id: 1,
    work_id: 'w1',
    provider_id: 'letterboxd',
    kind: 'watch',
    logged_at: '2026-03-14T21:45:00+00:00',
    logged_precision: 'exact',
    work: {
      id: 'w1',
      media_type: 'film',
      media_family: 'screen',
      title: 'The Fall Guy',
      image: '/api/v1/media/image/abc123',
      ...overrides,
    },
  } as Entry
}

const stubs = { RouterLink: true, LoggedAt: true }

describe('EntryList artwork', () => {
  it('renders the cache path the API supplied, never a platform URL', () => {
    const img = mount(EntryList, { props: { entries: [entry()] }, global: { stubs } }).find('img')
    expect(img.attributes('src')).toBe('/api/v1/media/image/abc123')
    // : a third-party host must never appear in page source.
    expect(img.attributes('src')).not.toMatch(/^https?:/)
  })

  it('renders no img when the work has no artwork', () => {
    const wrapper = mount(EntryList, {
      props: { entries: [entry({ image: null })] },
      global: { stubs },
    })
    expect(wrapper.find('img').exists()).toBe(false)
  })

  // Work.vue shows its own poster above the entry list; repeating it per row is noise.
  it('renders no img when the work is not named in the row', () => {
    const wrapper = mount(EntryList, {
      props: { entries: [entry()], showWork: false },
      global: { stubs },
    })
    expect(wrapper.find('img').exists()).toBe(false)
  })

  // Empty alt: the title is already beside it, so announcing the poster repeats the row.
  it('keeps the thumbnail out of the accessibility tree', () => {
    const wrapper = mount(EntryList, { props: { entries: [entry()] }, global: { stubs } })
    expect(wrapper.find('img').attributes('alt')).toBe('')
  })
})
