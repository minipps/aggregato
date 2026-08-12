import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => {
  const provider = {
    id: 'fixture',
    name: 'Fixture provider',
    enabled: false,
    status: 'disabled' as const,
    acquisition: 'export' as const,
    reviewed: true,
    capabilities: ['file_import'] as const,
    media_types: ['book'] as const,
    last_success_at: null,
    next_run_at: null,
    consecutive_failures: 0,
    last_error: null,
    file_pinned_settings: [],
    current_settings: {},
    last_check: {
      status: 'failure' as const,
      lineage_id: 'check-lineage',
      requested_at: '2026-08-08T10:00:00Z',
      completed_at: '2026-08-08T10:00:01Z',
      detail: 'token rejected',
      error_class: 'auth' as const,
    },
  }

  return {
    provider,
    providers: vi.fn().mockResolvedValue([provider]),
    checkProvider: vi.fn().mockResolvedValue({ lineage_id: 'new-check-lineage' }),
    importProviderFile: vi.fn(),
    latestProviderRun: vi.fn(),
    providerConfigSchema: vi.fn(),
    setProviderEnabled: vi.fn(),
    syncStatus: vi.fn().mockResolvedValue({
      type: 'snapshot',
      generated_at: '2026-08-08T10:00:00Z',
      providers: [],
      runs: [],
    }),
    syncProvider: vi.fn(),
    updateProviderConfig: vi.fn(),
  }
})

vi.mock('@/api/client', () => ({
  checkProvider: mocks.checkProvider,
  importProviderFile: mocks.importProviderFile,
  latestProviderRun: mocks.latestProviderRun,
  providerConfigSchema: mocks.providerConfigSchema,
  providers: mocks.providers,
  setProviderEnabled: mocks.setProviderEnabled,
  syncStatus: mocks.syncStatus,
  syncProvider: mocks.syncProvider,
  toProblem: (error: unknown) => ({
    type: 'about:blank',
    title: 'Something went wrong',
    status: 0,
    detail: error instanceof Error ? error.message : String(error),
  }),
  updateProviderConfig: mocks.updateProviderConfig,
}))

import { readonlyAccess } from '@/api/session'

import Providers from '../Providers.vue'

async function settle(): Promise<void> {
  await Promise.resolve()
  await nextTick()
}

describe('Providers provider checks', () => {
  afterEach(() => {
    readonlyAccess.value = false
    vi.clearAllMocks()
  })

  it('renders a latest diagnostic and allows checking a disabled provider', async () => {
    const wrapper = mount(Providers)
    await settle()

    expect(wrapper.text()).toContain('Latest provider check')
    expect(wrapper.text()).toContain('failure')
    expect(wrapper.text()).toContain('check-lineage')
    expect(wrapper.text()).toContain('token rejected')
    expect(wrapper.text()).toContain('auth')
    expect(wrapper.findAll('time').map((time) => time.attributes('datetime'))).toEqual([
      '2026-08-08T10:00:00.000Z',
      '2026-08-08T10:00:01.000Z',
    ])

    const button = wrapper.findAll('button').find((candidate) => candidate.text() === 'Check provider')
    expect(button).toBeDefined()
    expect(button?.attributes('disabled')).toBeUndefined()

    await button!.trigger('click')
    await settle()

    expect(mocks.checkProvider).toHaveBeenCalledWith('fixture')
    expect(wrapper.text()).toContain('Provider check queued.')
  })

  it('does not offer the write action to a read-only credential', async () => {
    readonlyAccess.value = true
    const wrapper = mount(Providers)
    await settle()

    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('Check provider')
  })
})
