/**
 * A read-only credential is offered no write control — not a disabled one it can click, and not a
 * keyboard shortcut that fires a request the server will refuse.
 */

import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

const decide = vi.fn()
const replay = vi.fn()

vi.mock('@/api/client', () => ({
  resolutionQueue: () => ({ next: vi.fn().mockResolvedValue({ items: [item], done: true }) }),
  decideResolution: (...args: unknown[]) => decide(...args),
  undoMerge: vi.fn(),
  ingestFailures: () => ({ next: vi.fn().mockResolvedValue({ items: [failure], done: true }) }),
  replayFailure: (...args: unknown[]) => replay(...args),
  session: vi.fn(),
}))

import { readonlyAccess } from '@/api/session'
import type { IngestFailure } from '@/api/types'

import IngestFailures from '../IngestFailures.vue'
import Resolution from '../Resolution.vue'

const item = {
  id: 7,
  subject: 'work' as const,
  provider_id: 'fixture',
  candidates: [{ id: 'candidate-id', label: 'Candidate', reason: 'same title and year' }],
  proposed: { title: 'Proposed' },
  created_at: '2026-01-01T00:00:00Z',
}

const failure: IngestFailure = {
  id: 3,
  provider_id: 'fixture',
  sync_run_id: 4,
  native_id: 'native-record-3',
  stage: 'normalize',
  error: 'boom',
  raw_payload: {},
  created_at: '2026-01-01T00:00:00Z',
  resolved_at: null,
}

async function settle(): Promise<void> {
  await Promise.resolve()
  await nextTick()
}

afterEach(() => {
  readonlyAccess.value = false
  vi.clearAllMocks()
})

describe('read-only access', () => {
  it('hides the resolution queue decisions and stops the shortcuts firing', async () => {
    readonlyAccess.value = true
    const wrapper = mount(Resolution)
    await settle()

    expect(wrapper.text()).toContain('Proposed')
    expect(wrapper.findAll('button').map((b) => b.text())).not.toContain('Ignore')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'i' }))
    await settle()
    expect(decide).not.toHaveBeenCalled()
  })

  it('hides the replay button but still shows the failure', async () => {
    readonlyAccess.value = true
    const wrapper = mount(IngestFailures)
    await settle()

    expect(wrapper.text()).toContain('boom')
    expect(wrapper.text()).not.toContain('Replay')
  })

  it('keeps the controls for a full credential', async () => {
    const wrapper = mount(IngestFailures)
    await settle()
    expect(wrapper.text()).toContain('Replay')
  })
})
