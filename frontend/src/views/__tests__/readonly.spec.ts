/**
 * A read-only credential is offered no write control — not a disabled one it can click, and not a
 * keyboard shortcut that fires a request the server will refuse.
 */

import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const decide = vi.fn()
const replay = vi.fn()
const mocks = vi.hoisted(() => ({
  archiveSettings: vi.fn(),
  ingestFailures: vi.fn(),
  syncDiagnostics: vi.fn(),
  updateArchiveSettings: vi.fn(),
  downloadArchive: vi.fn(),
  run: {
    id: 4,
    provider_id: 'fixture',
    lineage_id: 'lineage',
    attempt: 1,
    mode: 'incremental' as const,
    status: 'failure' as const,
    phase: 'finished' as const,
    started_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:01Z',
    items_seen: 1,
    items_written: 0,
    items_failed: 1,
    checkpoint_count: 0,
  },
}))

vi.mock('@/api/client', () => ({
  resolutionQueue: () => ({ next: vi.fn().mockResolvedValue({ items: [item], done: true }) }),
  decideResolution: (...args: unknown[]) => decide(...args),
  undoMerge: vi.fn(),
  archiveSettings: mocks.archiveSettings,
  updateArchiveSettings: (...args: unknown[]) => mocks.updateArchiveSettings(...args),
  downloadArchive: mocks.downloadArchive,
  ingestFailures: mocks.ingestFailures,
  replayFailure: (...args: unknown[]) => replay(...args),
  providers: vi.fn().mockResolvedValue([{ id: 'fixture', name: 'Fixture provider' }]),
  providerRuns: () => ({ next: vi.fn().mockResolvedValue({ items: [mocks.run], done: true }) }),
  syncRunDiagnostics: (...args: unknown[]) => mocks.syncDiagnostics(...args),
  session: vi.fn(),
}))

vi.mock('@/api/syncStream', () => ({ useSyncStream: () => ({ snapshot: { value: undefined } }) }))

import { readonlyAccess } from '@/api/session'
import type { ArchiveSettings, IngestFailure } from '@/api/types'

import IngestFailures from '../IngestFailures.vue'
import Resolution from '../Resolution.vue'
import Settings from '../Settings.vue'
import SyncHistory from '../SyncHistory.vue'

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

const archiveSettings: ArchiveSettings = {
  raw_payload_retention_days: 90,
  success_run_retention_days: 30,
  failure_run_retention_days: 180,
  image_cache_enabled: true,
  image_cache_configured_enabled: true,
  backup_supported: true,
  storage: { raw_payload_bytes: 0, image_cache_bytes: 0, database_bytes: 0 },
}

async function settle(): Promise<void> {
  await Promise.resolve()
  await nextTick()
}

beforeEach(() => {
  mocks.archiveSettings.mockResolvedValue(archiveSettings)
  mocks.ingestFailures.mockImplementation(() => ({
    next: vi.fn().mockResolvedValue({ items: [failure], done: true }),
  }))
  mocks.updateArchiveSettings.mockResolvedValue(archiveSettings)
})

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
    wrapper.unmount()
  })

  it('keeps failure payloads and their request operator-only', async () => {
    readonlyAccess.value = true
    const wrapper = mount(IngestFailures)
    await settle()

    expect(wrapper.text()).toContain('Ingest failure payloads are available only to an operator.')
    expect(wrapper.text()).not.toContain('boom')
    expect(wrapper.text()).not.toContain('Replay')
    expect(mocks.ingestFailures).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps write controls hidden while access is unknown', async () => {
    readonlyAccess.value = undefined
    const wrapper = mount(IngestFailures)
    await settle()

    expect(wrapper.text()).toContain('Operator access required')
    expect(wrapper.text()).not.toContain('Replay')
    expect(mocks.ingestFailures).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps the controls for a full credential', async () => {
    const wrapper = mount(IngestFailures)
    await settle()
    expect(wrapper.text()).toContain('Replay')
    wrapper.unmount()
  })

  it('shows run history but keeps protected diagnostics operator-only', async () => {
    readonlyAccess.value = true
    const wrapper = mount(SyncHistory)
    await settle()
    await wrapper.get('#history-provider').setValue('fixture')
    await settle()

    expect(wrapper.text()).toContain('Run diagnostics are available to operators only.')
    expect(wrapper.text()).toContain('failure')
    expect(wrapper.findAll('button').some((button) => button.text().includes('failure'))).toBe(false)
    expect(mocks.syncDiagnostics).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('settings', () => {
  it('locks a server-disabled image cache and omits it when saving retention', async () => {
    readonlyAccess.value = false
    const disabledSettings = {
      ...archiveSettings,
      image_cache_enabled: false,
      image_cache_configured_enabled: false,
    }
    mocks.archiveSettings.mockResolvedValueOnce(disabledSettings)
    mocks.updateArchiveSettings.mockResolvedValueOnce(disabledSettings)

    const wrapper = mount(Settings)
    await settle()

    const checkbox = wrapper.get('input[type="checkbox"]').element as HTMLInputElement
    expect(checkbox.disabled).toBe(true)
    expect(checkbox.checked).toBe(false)
    expect(wrapper.text()).toContain(
      'Image caching is disabled by server configuration. Set image_cache_enabled: true in the configuration to enable it.',
    )

    await wrapper.get('input[type="number"]').setValue(37)
    await wrapper.get('form').trigger('submit')
    await settle()

    expect(mocks.updateArchiveSettings).toHaveBeenCalledWith({
      raw_payload_retention_days: 37,
      success_run_retention_days: 30,
      failure_run_retention_days: 180,
    })
    wrapper.unmount()
  })
})
