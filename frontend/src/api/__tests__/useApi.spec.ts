import { nextTick } from 'vue'
import { describe, expect, it } from 'vitest'

import { usePaged, useRequest } from '../useApi'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

async function settle(): Promise<void> {
  await Promise.resolve()
  await nextTick()
}

describe('request state', () => {
  it('ignores stale success and error state after a newer request', async () => {
    const older = deferred<string>()
    const newer = deferred<string>()
    let calls = 0
    const request = useRequest(() => (calls++ === 0 ? older.promise : newer.promise))

    const reload = request.reload()
    newer.resolve('new result')
    await reload
    older.reject(new Error('stale failure'))
    await settle()

    expect(request.data.value).toBe('new result')
    expect(request.error.value).toBeUndefined()
    expect(request.loading.value).toBe(false)
  })

  it('ignores a page that finishes after restart', async () => {
    const older = deferred<{ items: string[]; done: boolean }>()
    const newer = deferred<{ items: string[]; done: boolean }>()
    let pagers = 0
    const request = usePaged(() => ({
      next: () => (pagers++ === 0 ? older.promise : newer.promise),
    }))

    const restart = request.restart()
    newer.resolve({ items: ['current filter'], done: false })
    await restart
    older.resolve({ items: ['stale filter'], done: true })
    await settle()

    expect(request.items.value).toEqual(['current filter'])
    expect(request.done.value).toBe(false)
    expect(request.loading.value).toBe(false)
  })

  it('keeps the cursor retryable after a page request fails', async () => {
    let calls = 0
    const request = usePaged(() => ({
      next: async () => {
        calls += 1
        if (calls === 1) throw new Error('temporary local API failure')
        return { items: ['retried page'], done: true }
      },
    }))
    await settle()

    expect(request.error.value?.detail).toBe('temporary local API failure')
    expect(request.done.value).toBe(false)

    await request.loadMore()
    expect(request.items.value).toEqual(['retried page'])
    expect(request.done.value).toBe(true)
  })
})
