import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SyncSnapshot } from '../types'

const mocks = vi.hoisted(() => ({
  syncStatus: vi.fn().mockResolvedValue({ type: 'snapshot', generated_at: '', providers: [], runs: [] }),
  session: vi.fn(),
}))

vi.mock('../client', () => ({
  session: mocks.session,
  syncStatus: mocks.syncStatus,
  toProblem: (error: unknown) => ({
    type: 'about:blank',
    title: 'Something went wrong',
    status: 0,
    detail: error instanceof Error ? error.message : String(error),
  }),
}))

import { sessionInfo } from '../session'
import { useSyncStream } from '../syncStream'

let stream: ReturnType<typeof useSyncStream> | undefined

class MockWebSocket {
  static instances: MockWebSocket[] = []
  onopen: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this)
  }

  close(): void {
    this.onclose?.(new CloseEvent('close'))
  }
}

const Harness = defineComponent({
  setup() {
    stream = useSyncStream()
    return () => h('div')
  },
})

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise })
  return { promise, resolve }
}

afterEach(() => {
  sessionInfo.value = undefined
  stream = undefined
  MockWebSocket.instances = []
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('sync stream authentication', () => {
  it('uses HTTP refresh without opening or reconnecting a public websocket', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', MockWebSocket)
    sessionInfo.value = { via: 'public', readonly: true }
    const wrapper = mount(Harness)
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(15000)

    expect(mocks.syncStatus).toHaveBeenCalled()
    expect(MockWebSocket.instances).toHaveLength(0)
    wrapper.unmount()
  })

  it('opens the socket after a non-public session is confirmed', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', MockWebSocket)
    sessionInfo.value = { via: 'cookie', readonly: true }
    const wrapper = mount(Harness)
    await vi.advanceTimersByTimeAsync(0)

    expect(MockWebSocket.instances).toHaveLength(1)
    wrapper.unmount()
  })

  it('ignores callbacks and fallback data from an obsolete session socket', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', MockWebSocket)
    sessionInfo.value = { via: 'cookie', readonly: true }
    const firstFallback = deferred<SyncSnapshot>()
    mocks.syncStatus.mockReturnValueOnce(firstFallback.promise)

    const wrapper = mount(Harness)
    await vi.advanceTimersByTimeAsync(0)
    const oldSocket = MockWebSocket.instances[0]!
    oldSocket.onopen?.(new Event('open'))
    expect(stream?.connected.value).toBe(true)

    sessionInfo.value = { via: 'public', readonly: true }
    await vi.advanceTimersByTimeAsync(0)

    const obsolete: SyncSnapshot = {
      type: 'snapshot',
      generated_at: 'obsolete-session',
      providers: [],
      runs: [],
    }
    oldSocket.onopen?.(new Event('open'))
    oldSocket.onmessage?.({ data: JSON.stringify(obsolete) } as MessageEvent<string>)
    oldSocket.onerror?.(new Event('error'))
    oldSocket.onclose?.(new CloseEvent('close'))
    expect(stream?.connected.value).toBe(false)
    expect(stream?.snapshot.value).toBeUndefined()
    expect(stream?.error.value).toBeUndefined()

    firstFallback.resolve({ ...obsolete, generated_at: 'old-http-session' })
    await vi.advanceTimersByTimeAsync(0)
    expect(stream?.snapshot.value).toBeUndefined()

    await vi.advanceTimersByTimeAsync(5000)
    expect(mocks.syncStatus).toHaveBeenCalledTimes(2)
    expect(MockWebSocket.instances).toHaveLength(1)
    wrapper.unmount()

    const callsAtUnmount = mocks.syncStatus.mock.calls.length
    oldSocket.onmessage?.({ data: JSON.stringify(obsolete) } as MessageEvent<string>)
    oldSocket.onclose?.(new CloseEvent('close'))
    await vi.advanceTimersByTimeAsync(15000)
    expect(mocks.syncStatus).toHaveBeenCalledTimes(callsAtUnmount)
    expect(MockWebSocket.instances).toHaveLength(1)
  })
})
