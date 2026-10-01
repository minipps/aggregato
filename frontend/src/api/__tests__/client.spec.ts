import { afterEach, describe, expect, it, vi } from 'vitest'

import { checkProvider, downloadArchive, importProviderFile, ProblemError, request } from '../client'

describe('checkProvider', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('queues an encoded provider check through the CSRF-aware request wrapper', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ lineage_id: 'check-lineage' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.spyOn(document, 'cookie', 'get').mockReturnValue('aggregato_csrf=csrf-value')

    await expect(checkProvider('provider/one')).resolves.toEqual({ lineage_id: 'check-lineage' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/providers/provider%2Fone/check',
      expect.objectContaining({
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          Accept: 'application/json',
          'X-CSRF-Token': 'csrf-value',
        },
      }),
    )
  })
})

describe('shared request handling', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('sends uploads as FormData through the CSRF-aware wrapper', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 202,
      json: async () => ({ lineage_id: 'import-lineage' }),
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.spyOn(document, 'cookie', 'get').mockReturnValue('aggregato_csrf=csrf-value')
    const file = new File(['entry'], 'history.csv', { type: 'text/csv' })

    await expect(importProviderFile('provider/one', file)).resolves.toEqual({ lineage_id: 'import-lineage' })

    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/providers/provider%2Fone/import')
    expect(options.body).toBeInstanceOf(FormData)
    expect(options.headers).toEqual({ Accept: 'application/json', 'X-CSRF-Token': 'csrf-value' })
  })

  it('decodes archive failures as problem details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      statusText: 'Service unavailable',
      headers: new Headers({ 'Content-Type': 'application/problem+json' }),
      json: async () => ({ title: 'Export unavailable', detail: 'SQLite is required', status: 503 }),
    }))

    await expect(downloadArchive()).rejects.toBeInstanceOf(ProblemError)
  })

  it('decodes successful archive responses as blobs', async () => {
    const archive = new Blob(['zip data'])
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, blob: async () => archive }))

    await expect(request<Blob>('GET', '/export', { responseType: 'blob' })).resolves.toBe(archive)
  })
})
