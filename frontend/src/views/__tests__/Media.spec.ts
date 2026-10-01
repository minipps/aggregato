import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import type { Entry, EntryQuery, MediaType } from '@/api/types'

const mocks = vi.hoisted(() => ({ entries: vi.fn() }))

vi.mock('@/api/client', () => ({ entries: mocks.entries }))

import Media from '../Media.vue'

const types: MediaType[] = [
  'anime',
  'manga',
  'film',
  'tv',
  'book',
  'comic',
  'album',
  'track',
  'game',
  'other',
]

function covers(type: MediaType): Entry[] {
  return Array.from({ length: 5 }, (_, index) => ({
    id: index,
    work_id: `${type}-${index}`,
    provider_id: 'fixture',
    kind: 'finish',
    logged_at: '2026-09-29T00:00:00Z',
    logged_precision: 'day',
    work: {
      id: `${type}-${index}`,
      media_type: type,
      media_family: 'other',
      title: `${type} ${index}`,
      image: `/api/v1/media/image/${type}-${index}`,
    },
  }))
}

describe('Media gallery', () => {
  it('loads five completed covers for every media type', async () => {
    mocks.entries.mockImplementation((query: EntryQuery = {}) => ({
      next: vi.fn().mockResolvedValue({ items: covers(query.media_type ?? 'other'), done: true }),
    }))

    const wrapper = mount(Media, {
      global: { stubs: { LoggedAt: true, RouterLink: { template: '<a><slot /></a>' } } },
    })
    await flushPromises()

    const rows = wrapper.findAll('.media-grid')
    expect(rows).toHaveLength(types.length)
    expect(rows.every((row) => row.findAll('img').length === 5)).toBe(true)
    for (const media_type of types) {
      expect(mocks.entries).toHaveBeenCalledWith({ media_type, status: 'completed', limit: 5 })
    }
    expect(wrapper.find('img').attributes('alt')).toBe('')
  })
})
