/**
 * Live sync state shared by the operational views.
 *
 * The websocket is a read-only view of durable database state.  A small HTTP refresh is kept as a
 * fallback for older reverse proxies and for browsers without websocket support; the UI still has a
 * useful state while the live connection is being established.
 */

import { onMounted, onUnmounted, ref, shallowRef } from 'vue'

import { syncStatus, toProblem } from './client'
import type { Problem, SyncSnapshot } from './types'

const RECONNECT_DELAY_MS = 3000
const FALLBACK_DELAY_MS = 5000

function websocketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/v1/ws/sync`
}

function isSnapshot(value: unknown): value is SyncSnapshot {
  return (
    typeof value === 'object' &&
    value !== null &&
    'type' in value &&
    value.type === 'snapshot' &&
    'providers' in value &&
    Array.isArray(value.providers) &&
    'runs' in value &&
    Array.isArray(value.runs)
  )
}

export function useSyncStream(): {
  snapshot: ReturnType<typeof shallowRef<SyncSnapshot | undefined>>
  connected: ReturnType<typeof ref<boolean>>
  error: ReturnType<typeof ref<Problem | undefined>>
} {
  const snapshot = shallowRef<SyncSnapshot | undefined>(undefined)
  const connected = ref(false)
  const error = ref<Problem | undefined>(undefined)
  let socket: WebSocket | undefined
  let stopped = false
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined
  let fallbackTimer: ReturnType<typeof setTimeout> | undefined

  async function loadFallback(): Promise<void> {
    if (stopped || connected.value || typeof syncStatus !== 'function') return
    try {
      const next = await syncStatus()
      // A websocket may have opened while the HTTP request was in flight. Never let that stale
      // fallback response overwrite a newer live snapshot.
      if (stopped || connected.value) return
      snapshot.value = next
      error.value = undefined
    } catch (caught) {
      if (!stopped && !connected.value) error.value = toProblem(caught)
    }
    if (!stopped && !connected.value) {
      fallbackTimer = setTimeout(() => void loadFallback(), FALLBACK_DELAY_MS)
    }
  }

  function connect(): void {
    if (stopped || typeof WebSocket === 'undefined') return
    socket = new WebSocket(websocketUrl())
    socket.onopen = () => {
      connected.value = true
      error.value = undefined
      if (fallbackTimer !== undefined) {
        clearTimeout(fallbackTimer)
        fallbackTimer = undefined
      }
    }
    socket.onmessage = (event: MessageEvent<string>) => {
      try {
        const parsed: unknown = JSON.parse(event.data)
        if (isSnapshot(parsed)) snapshot.value = parsed
      } catch (caught) {
        error.value = toProblem(caught)
      }
    }
    socket.onerror = () => {
      error.value = {
        type: 'about:blank',
        title: 'Live sync updates unavailable',
        status: 0,
        detail: 'The page will continue checking the sync status in the background.',
      }
      socket?.close()
    }
    socket.onclose = () => {
      connected.value = false
      socket = undefined
      if (!stopped) {
        reconnectTimer = setTimeout(connect, RECONNECT_DELAY_MS)
        void loadFallback()
      }
    }
  }

  onMounted(() => {
    void loadFallback()
    connect()
  })

  onUnmounted(() => {
    stopped = true
    if (reconnectTimer !== undefined) clearTimeout(reconnectTimer)
    if (fallbackTimer !== undefined) clearTimeout(fallbackTimer)
    socket?.close()
    socket = undefined
  })

  return { snapshot, connected, error }
}
