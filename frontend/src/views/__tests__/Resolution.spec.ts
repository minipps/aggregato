import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const next = vi.fn()
const decide = vi.fn()

vi.mock('@/api/client', () => ({
  resolutionQueue: () => ({ next }),
  decideResolution: (...args: unknown[]) => decide(...args),
  undoMerge: vi.fn(),
}))

import Resolution from '../Resolution.vue'

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
    next.mockResolvedValue({ items: [item], done: true })
    decide.mockResolvedValue({ id: 12 })
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
  })
})
