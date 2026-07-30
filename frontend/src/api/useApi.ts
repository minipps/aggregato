/**
 * Loading / error / data plumbing, shared by every view.
 *
 * Four views need the same three states, and Constitution III requires them to look the same in all
 * four. That is the second occurrence rule met twice over — but nothing more general is here: no
 * store, no cache, no request deduplication.
 */

import { shallowRef, ref, type Ref, type ShallowRef } from 'vue'

import { toProblem, type Pager } from './client'
import type { Problem } from './types'

export interface Request<T> {
  data: ShallowRef<T | undefined>
  loading: Ref<boolean>
  error: Ref<Problem | undefined>
  reload: () => Promise<void>
}

/**
 * Run `fetcher` immediately and expose its state.
 *
 * Inputs: a function performing one API call. Failure modes: none — a rejection lands in `error` as
 * a problem detail rather than propagating, so a failing panel degrades itself only.
 */
export function useRequest<T>(fetcher: () => Promise<T>): Request<T> {
  const data = shallowRef<T | undefined>(undefined)
  const loading = ref(false)
  const error = ref<Problem | undefined>(undefined)

  async function reload(): Promise<void> {
    loading.value = true
    error.value = undefined
    try {
      data.value = await fetcher()
    } catch (caught) {
      error.value = toProblem(caught)
    } finally {
      loading.value = false
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

/**
 * Accumulate a cursor-paginated collection.
 *
 * Inputs: a factory returning a fresh {@link Pager}; `restart` calls it again, since a cursor walk
 * cannot be rewound. Failure modes: none — rejections land in `error`.
 */
export function usePaged<T>(makePager: () => Pager<T>): PagedRequest<T> {
  const items = shallowRef<T[]>([])
  const loading = ref(false)
  const error = ref<Problem | undefined>(undefined)
  const done = ref(false)
  let current = makePager()

  async function loadMore(): Promise<void> {
    if (loading.value || done.value) return
    loading.value = true
    error.value = undefined
    try {
      const page = await current.next()
      items.value = [...items.value, ...page.items]
      done.value = page.done
    } catch (caught) {
      error.value = toProblem(caught)
      // Stop walking: the cursor is unusable after a failed page, and retrying it in a loop would
      // hammer a provider that is already unhappy. `restart` is the way back.
      done.value = true
    } finally {
      loading.value = false
    }
  }

  async function restart(): Promise<void> {
    current = makePager()
    items.value = []
    done.value = false
    loading.value = false
    await loadMore()
  }

  void loadMore()
  return { items, loading, error, done, loadMore, restart }
}
