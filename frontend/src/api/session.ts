/**
 * Whether the current credential may only read.
 *
 * The server refuses every write from a read-only credential regardless of what the UI renders
 * (`deps.py`), so this flag is not a security boundary — it exists so a read-only viewer is not
 * offered buttons that can only fail. Fetched once per page load and after signing in.
 */

import { ref, shallowRef } from 'vue'

import { session } from './client'
import type { Session } from './types'

/** Unknown until the server confirms this browser's credential. */
export const readonlyAccess = ref<boolean | undefined>(undefined)
export const sessionInfo = shallowRef<Session | undefined>(undefined)

/**
 * Refresh {@link readonlyAccess} from `GET /auth/session`.
 *
 * Never throws. A failed probe leaves access unknown, so write controls stay hidden.
 */
export async function loadSession(): Promise<void> {
  try {
    const current = await session()
    sessionInfo.value = current
    readonlyAccess.value = current.readonly
  } catch {
    sessionInfo.value = undefined
    readonlyAccess.value = undefined
  }
}
