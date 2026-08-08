import { afterEach, describe, expect, it, vi } from 'vitest'

import { checkProvider } from '../client'

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
