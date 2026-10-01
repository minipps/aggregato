/**
 * Live sync state shared by the operational views.
 *
 * The websocket is a read-only view of durable database state.  A small HTTP refresh is kept as a
 * fallback for older reverse proxies and for browsers without websocket support; the UI still has a
 * useful state while the live connection is being established.
 */

import { onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'

import { syncStatus, toProblem } from './client'
import { sessionInfo } from './session'
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
  let mounted = false
  let sessionGeneration = 0
  let fallbackLoading = false
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined
  let fallbackTimer: ReturnType<typeof setTimeout> | undefined

  function canConnect(): boolean {
    return sessionInfo.value !== undefined && sessionInfo.value.via !== 'public'
  }

  function scheduleFallback(): void {
    if (stopped || connected.value || fallbackTimer !== undefined) return
    fallbackTimer = setTimeout(() => {
      fallbackTimer = undefined
      void loadFallback()
    }, FALLBACK_DELAY_MS)
  }

  async function loadFallback(): Promise<void> {
    if (stopped || connected.value || fallbackLoading || typeof syncStatus !== 'function') return
    fallbackLoading = true
    const requestGeneration = sessionGeneration
    try {
      const next = await syncStatus()
      if (stopped || connected.value || requestGeneration !== sessionGeneration) return
      snapshot.value = next
      error.value = undefined
    } catch (caught) {
      if (!stopped && !connected.value && requestGeneration === sessionGeneration) {
        error.value = toProblem(caught)
      }
    } finally {
      fallbackLoading = false
      scheduleFallback()
    }
  }

  function connect(): void {
    if (stopped || !mounted || !canConnect() || socket || typeof WebSocket === 'undefined') return
    const currentSession = sessionInfo.value
    const generation = sessionGeneration
    const active = new WebSocket(websocketUrl())
    socket = active
    active.onopen = () => {
      if (
        stopped || socket !== active || !canConnect()
        || sessionGeneration !== generation || sessionInfo.value !== currentSession
      ) return
      connected.value = true
      error.value = undefined
      if (fallbackTimer !== undefined) {
        clearTimeout(fallbackTimer)
        fallbackTimer = undefined
      }
    }
    active.onmessage = (event: MessageEvent<string>) => {
      if (
        stopped || socket !== active || !canConnect() || !connected.value
        || sessionGeneration !== generation || sessionInfo.value !== currentSession
      ) return
      try {
        const parsed: unknown = JSON.parse(event.data)
        if (isSnapshot(parsed)) snapshot.value = parsed
      } catch (caught) {
        error.value = toProblem(caught)
      }
    }
    active.onerror = () => {
      if (
        stopped || socket !== active || !canConnect()
        || sessionGeneration !== generation || sessionInfo.value !== currentSession
      ) return
      error.value = {
        type: 'about:blank',
        title: 'Live sync updates unavailable',
        status: 0,
        detail: 'The page will continue checking the sync status in the background.',
      }
      active.close()
    }
    active.onclose = () => {
      if (
        stopped || socket !== active || !canConnect()
        || sessionGeneration !== generation || sessionInfo.value !== currentSession
      ) return
      socket = undefined
      connected.value = false
      if (canConnect() && reconnectTimer === undefined) {
        reconnectTimer = setTimeout(() => {
          reconnectTimer = undefined
          connect()
        }, RECONNECT_DELAY_MS)
      }
      void loadFallback()
    }
  }

  watch(sessionInfo, (current) => {
    if (!mounted) return
    sessionGeneration += 1
    if (reconnectTimer !== undefined) clearTimeout(reconnectTimer)
    reconnectTimer = undefined
    if (fallbackTimer !== undefined) clearTimeout(fallbackTimer)
    fallbackTimer = undefined
    const active = socket
    socket = undefined
    connected.value = false
    snapshot.value = undefined
    error.value = undefined
    active?.close()
    void loadFallback()
    if (current && current.via !== 'public') connect()
  })

  onMounted(() => {
    mounted = true
    void loadFallback()
    connect()
  })

  onUnmounted(() => {
    stopped = true
    sessionGeneration += 1
    if (reconnectTimer !== undefined) clearTimeout(reconnectTimer)
    if (fallbackTimer !== undefined) clearTimeout(fallbackTimer)
    const active = socket
    socket = undefined
    connected.value = false
    active?.close()
  })

  return { snapshot, connected, error }
}
