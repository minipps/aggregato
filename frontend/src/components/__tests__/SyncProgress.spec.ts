import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { SyncRun } from '@/api/types'

import SyncProgress from '../SyncProgress.vue'

function run(overrides: Partial<SyncRun> = {}): SyncRun {
  return {
    id: 7,
    provider_id: 'fixture',
    lineage_id: 'lineage',
    attempt: 1,
    mode: 'incremental',
    status: 'running',
    phase: 'fetching',
    started_at: '2026-08-11T12:00:00Z',
    updated_at: '2026-08-11T12:00:01Z',
    items_seen: 4,
    items_written: 3,
    items_failed: 1,
    progress_total: 10,
    progress_percent: 40,
    checkpoint_count: 2,
    ...overrides,
  }
}

describe('SyncProgress', () => {
  it('renders a determinate provider total and counters', () => {
    const wrapper = mount(SyncProgress, { props: { run: run() } })

    expect(wrapper.find('progress').attributes('value')).toBe('40')
    expect(wrapper.text()).toContain('40% · 4 of 10 records')
    expect(wrapper.text()).toContain('Written3')
    expect(wrapper.text()).toContain('Checkpoints2')
  })

  it('renders indeterminate progress when the provider has no total', () => {
    const wrapper = mount(
      SyncProgress,
      { props: { run: run({ progress_total: null, progress_percent: null }) } },
    )

    expect(wrapper.find('[role="progressbar"]').exists()).toBe(true)
    expect(wrapper.find('progress').exists()).toBe(false)
    expect(wrapper.text()).toContain('4 records seen · total not reported by provider')
  })
})
