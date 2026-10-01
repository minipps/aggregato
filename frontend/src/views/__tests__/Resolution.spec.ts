import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const next = vi.fn()
const decide = vi.fn()
const undo = vi.fn()

vi.mock('@/api/client', () => ({
  resolutionQueue: () => ({ next }),
  decideResolution: (...args: unknown[]) => decide(...args),
  undoMerge: (...args: unknown[]) => undo(...args),
}))

import Resolution from '../Resolution.vue'
import { readonlyAccess } from '@/api/session'

const item = {
  id: 7,
  subject: 'work' as const,
  provider_id: 'fixture',
  candidates: [{ id: 'candidate-id', label: 'Candidate', reason: 'same title and year' }],
  proposed: { title: 'Proposed' },
  created_at: '2026-01-01T00:00:00Z',
}

describe('Resolution', () => {
  beforeEach(() => {
    readonlyAccess.value = false
    next.mockResolvedValue({ items: [item], done: true })
    decide.mockResolvedValue({ id: 12 })
    undo.mockReset()
  })

  it('exposes keyboard shortcuts and operable named controls', async () => {
    const wrapper = mount(Resolution)
    await Promise.resolve()
    await nextTick()
    expect(wrapper.text()).toContain('↑/↓ or j/k')
    const link = wrapper.get('button').element
    expect(link.type).toBe('button')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: '1' }))
    await Promise.resolve()
    await nextTick()
    expect(decide).toHaveBeenCalledWith(7, 'linked', 'candidate-id')
    expect(wrapper.find('[role="status"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('keeps the undo identifier separate from the notice text', async () => {
    const wrapper = mount(Resolution)
    await Promise.resolve()
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: '1' }))
    await Promise.resolve()
    await nextTick()
    expect(wrapper.get('[role="status"]').text()).toBe('Decision saved.')

    await wrapper.findAll('button').find((button) => button.text() === 'Undo')!.trigger('click')
    await Promise.resolve()
    await nextTick()

    expect(undo).toHaveBeenCalledWith(12)
    expect(wrapper.get('[role="status"]').text()).toContain('Decision undone.')
    wrapper.unmount()
  })
})
