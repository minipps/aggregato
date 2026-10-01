import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
      status: 'failure' as 'pending' | 'failure' | 'success',
      lineage_id: 'check-lineage',
      requested_at: '2026-08-08T10:00:00Z',
      completed_at: '2026-08-08T10:00:01Z',
      detail: 'token rejected',
      error_class: 'auth' as 'auth' | null,
    },
  }
  const secondProvider = { ...provider, id: 'fixture-two', name: 'Second provider', last_check: null }

  return {
    provider,
    secondProvider,
    providers: vi.fn().mockResolvedValue([provider, secondProvider]),
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
  beforeEach(() => {
    readonlyAccess.value = false
    mocks.secondProvider.last_check = null
    mocks.provider.last_check = {
      status: 'failure',
      lineage_id: 'check-lineage',
      requested_at: '2026-08-08T10:00:00Z',
      completed_at: '2026-08-08T10:00:01Z',
      detail: 'token rejected',
      error_class: 'auth',
    }
    mocks.providers.mockResolvedValue([mocks.provider, mocks.secondProvider])
    mocks.checkProvider.mockImplementation(async () => {
      mocks.provider.last_check = {
        status: 'success',
        lineage_id: 'new-check-lineage',
        requested_at: '2026-08-08T10:01:00Z',
        completed_at: '2026-08-08T10:01:01Z',
        detail: 'credentials accepted',
        error_class: null,
      }
      return { lineage_id: 'new-check-lineage' }
    })
  })

  afterEach(() => {
    readonlyAccess.value = false
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('renders a latest diagnostic and allows checking a disabled provider', async () => {
    vi.useFakeTimers()
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

    const pendingProvider = {
      ...mocks.provider,
      last_check: {
        status: 'pending' as const,
        lineage_id: 'new-check-lineage',
        requested_at: '2026-08-08T10:01:00Z',
        completed_at: null,
        detail: null,
        error_class: null,
      },
    }
    const completedProvider = {
      ...mocks.provider,
      last_check: {
        ...pendingProvider.last_check,
        status: 'success' as const,
        completed_at: '2026-08-08T10:01:01Z',
        detail: 'credentials accepted',
      },
    }
    mocks.providers.mockReset()
    mocks.providers
      .mockResolvedValueOnce([pendingProvider, mocks.secondProvider])
      .mockResolvedValueOnce([completedProvider, mocks.secondProvider])
    mocks.checkProvider.mockResolvedValueOnce({ lineage_id: 'new-check-lineage' })

    await button!.trigger('click')
    await settle()

    expect(mocks.checkProvider).toHaveBeenCalledWith('fixture')
    expect(wrapper.text()).toContain('Provider check queued.')
    await vi.advanceTimersByTimeAsync(5000)
    await settle()

    expect(wrapper.text()).toContain('Provider check success.')
    expect(wrapper.text()).toContain('credentials accepted')
    wrapper.unmount()
  })

  it('does not offer the write action to a read-only credential', async () => {
    readonlyAccess.value = true
    const wrapper = mount(Providers)
    await settle()

    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('Check provider')
  })

  it('stops checking after the provider view unmounts', async () => {
    vi.useFakeTimers()
    const pendingProvider = {
      ...mocks.provider,
      last_check: {
        status: 'pending' as const,
        lineage_id: 'new-check-lineage',
        requested_at: '2026-08-08T10:01:00Z',
        completed_at: null,
        detail: null,
        error_class: null,
      },
    }
    mocks.providers.mockReset()
    mocks.providers
      .mockResolvedValueOnce([mocks.provider, mocks.secondProvider])
      .mockResolvedValueOnce([pendingProvider, mocks.secondProvider])
    mocks.checkProvider.mockResolvedValueOnce({ lineage_id: 'new-check-lineage' })

    const wrapper = mount(Providers)
    await vi.advanceTimersByTimeAsync(0)
    const check = wrapper.findAll('button').find((button) => button.text() === 'Check provider')
    await check!.trigger('click')
    await settle()
    const requests = mocks.providers.mock.calls.length

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(60_000)

    expect(mocks.providers).toHaveBeenCalledTimes(requests)
  })

  it('disables actions on every card while a provider action is pending', async () => {
    let finish!: () => void
    mocks.setProviderEnabled.mockImplementation(() => new Promise<void>((resolve) => { finish = resolve }))
    const wrapper = mount(Providers)
    await settle()

    const enable = wrapper.findAll('button').filter((button) => button.text() === 'Enable')
    expect(enable).toHaveLength(2)
    await enable[0]!.trigger('click')
    await nextTick()

    expect(wrapper.findAll('button').filter((button) => button.text() === 'Enable')
      .every((button) => button.attributes('disabled') !== undefined)).toBe(true)

    finish()
    await settle()
  })
})
