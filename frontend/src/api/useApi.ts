/** Shared loading, error, and cursor state for API-backed views. */

import { shallowRef, ref, type Ref, type ShallowRef } from 'vue'

import { toProblem, type Pager } from './client'
import type { Problem } from './types'

export interface Request<T> {
  data: ShallowRef<T | undefined>
  loading: Ref<boolean>
  error: Ref<Problem | undefined>
  reload: () => Promise<void>
}

/** Run `fetcher` immediately; store rejected calls as problem state. */
export function useRequest<T>(fetcher: () => Promise<T>): Request<T> {
  const data = shallowRef<T | undefined>(undefined)
  const loading = ref(false)
  const error = ref<Problem | undefined>(undefined)
  let generation = 0

  async function reload(): Promise<void> {
    const request = ++generation
    loading.value = true
    error.value = undefined
    try {
      const result = await fetcher()
      if (request === generation) data.value = result
    } catch (caught) {
      if (request === generation) error.value = toProblem(caught)
    } finally {
      if (request === generation) loading.value = false
    }
  }

  void reload()
  return { data, loading, error, reload }
}

export interface PagedRequest<T> {
  items: ShallowRef<T[]>
  loading: Ref<boolean>
  error: Ref<Problem | undefined>
  done: Ref<boolean>
  /** Fetch the next cursor page and append it. */
  loadMore: () => Promise<void>
  /** Start over from a fresh pager — what a filter change does. */
  restart: () => Promise<void>
}

/** Accumulate cursor pages; `restart` creates a new pager for changed filters. */
export function usePaged<T>(makePager: () => Pager<T>): PagedRequest<T> {
  const items = shallowRef<T[]>([])
  const loading = ref(false)
  const error = ref<Problem | undefined>(undefined)
  const done = ref(false)
  let current = makePager()
  let generation = 0

  async function loadMore(): Promise<void> {
    if (loading.value || done.value) return
    const request = generation
    loading.value = true
    error.value = undefined
    try {
      const page = await current.next()
      if (request !== generation) return
      items.value = [...items.value, ...page.items]
      done.value = page.done
    } catch (caught) {
      if (request !== generation) return
      error.value = toProblem(caught)
      // A failed local page leaves the cursor unchanged, so callers can retry it.
    } finally {
      if (request === generation) loading.value = false
    }
  }

  async function restart(): Promise<void> {
    generation += 1
    current = makePager()
    items.value = []
    done.value = false
    error.value = undefined
    loading.value = false
    await loadMore()
  }

  void loadMore()
  return { items, loading, error, done, loadMore, restart }
}
