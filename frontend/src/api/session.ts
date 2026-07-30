/**
 * Whether the current credential may only read.
 *
 * The server refuses every write from a read-only credential regardless of what the UI renders
 * (`deps.py`), so this flag is not a security boundary — it exists so a read-only viewer is not
 * offered buttons that can only fail. Fetched once per page load and after signing in.
 */

import { ref } from 'vue'

import { session } from './client'

export const readonlyAccess = ref(false)

/**
 * Refresh {@link readonlyAccess} from `GET /auth/session`.
 *
 * Never throws: an unauthenticated caller is already being routed to the login view by `request`,
 * and assuming full access on a failed probe costs nothing — the server still says no.
 */
export async function loadSession(): Promise<void> {
  try {
    readonlyAccess.value = (await session()).readonly
  } catch {
    readonlyAccess.value = false
  }
}
