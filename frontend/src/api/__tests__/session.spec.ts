import { afterEach, describe, expect, it, vi } from 'vitest'

const sessionMock = vi.hoisted(() => vi.fn())

vi.mock('../client', () => ({ session: sessionMock }))

import { loadSession, readonlyAccess, sessionInfo } from '../session'

afterEach(() => {
  readonlyAccess.value = undefined
  sessionInfo.value = undefined
  vi.clearAllMocks()
})

describe('session access state', () => {
  it('keeps writes hidden until an operator session is confirmed', async () => {
    expect(readonlyAccess.value).toBeUndefined()
    sessionMock.mockResolvedValueOnce({ via: 'cookie', readonly: false })

    await loadSession()

    expect(readonlyAccess.value).toBe(false)
    expect(sessionInfo.value).toEqual({ via: 'cookie', readonly: false })
  })

  it('leaves access unknown when the session probe fails', async () => {
    readonlyAccess.value = false
    sessionInfo.value = { via: 'cookie', readonly: false }
    sessionMock.mockRejectedValueOnce(new Error('probe failed'))

    await loadSession()

    expect(readonlyAccess.value).toBeUndefined()
    expect(sessionInfo.value).toBeUndefined()
  })
})
