import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  entries: vi.fn(() => ({ next: vi.fn().mockResolvedValue({ items: [], done: true }) })),
  providers: vi.fn().mockResolvedValue([]),
}))

vi.mock('@/api/client', () => ({ entries: mocks.entries, providers: mocks.providers }))

import Log from '../Log.vue'

async function settle(): Promise<void> {
  await Promise.resolve()
  await nextTick()
}

describe('log date filters', () => {
  it('includes fractional timestamps through the end of the selected UTC day', async () => {
    const wrapper = mount(Log)
    await settle()
    await wrapper.get('#filter-from').setValue('2026-12-31')
    await wrapper.get('#filter-to').setValue('2026-12-31')
    await wrapper.get('form').trigger('submit')
    await settle()

    expect(mocks.entries).toHaveBeenLastCalledWith({
      from: '2026-12-31T00:00:00Z',
      to: '2026-12-31T23:59:59.999999Z',
    })
    expect(wrapper.text()).toContain('Date filters use UTC days.')
  })
})
