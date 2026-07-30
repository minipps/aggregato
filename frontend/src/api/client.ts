/**
 * The one fetch wrapper, plus the typed calls US1 needs.
 *
 * Every request in the UI goes through {@link request}, so authentication, CSRF, and error shape are
 * decided once. Constitution III requires uniform error presentation; that is only achievable if
 * there is a single failure path, so `request` rejects with a {@link Problem} for *every* failure —
 * HTTP problem detail, unexpected non-JSON body, or a dead network alike.
 */

import { router } from '@/router'

import type {
  CheckResult,
  Creator,
  CreatorDetail,
  Entry,
  EntryQuery,
  Health,
  Page,
  Problem,
  Provider,
  SyncRun,
  IngestFailure,
  MergeLogEntry,
  ResolutionItem,
  WorkDetail,
} from './types'

/** Matches `servers:` in openapi.yaml. The SPA and the API share an origin in production. */
const BASE = '/api/v1'

/** JS-readable half of the double-submit CSRF pair (`deps.py`: `CSRF_COOKIE`). */
const CSRF_COOKIE = 'aggregato_csrf'
const CSRF_HEADER = 'X-CSRF-Token'

/** Cookie-authenticated writes must echo the CSRF token; bearer requests are exempt (deps.py). */
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

const LOGIN_PATH = '/auth/session'

type QueryValue = string | number | boolean | undefined | null

export interface RequestOptions {
  query?: Record<string, QueryValue>
  body?: unknown
  /** Extra headers. Used only by the login call, which presents the API token as a bearer. */
  headers?: Record<string, string>
}

/**
 * Error carrying an RFC 9457 problem detail.
 *
 * Inputs: the parsed or synthesised problem. Views render `problem` and never a raw status code.
 */
export class ProblemError extends Error {
  readonly problem: Problem

  constructor(problem: Problem) {
    super(problem.detail ?? problem.title)
    this.name = 'ProblemError'
    this.problem = problem
  }
}

/**
 * The problem detail behind any thrown value, so a `catch` block has one shape to render.
 *
 * Inputs: whatever was caught. Never throws: an unrecognised value becomes a generic 0-status
 * problem rather than a blank view (Constitution III).
 */
export function toProblem(error: unknown): Problem {
  if (error instanceof ProblemError) return error.problem
  return {
    type: 'about:blank',
    title: 'Something went wrong',
    status: 0,
    detail: error instanceof Error ? error.message : String(error),
  }
}

function readCookie(name: string): string | null {
  // document.cookie is the only place the CSRF value exists: the session cookie is HttpOnly.
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  const value = match?.[1]
  return value === undefined ? null : decodeURIComponent(value)
}

function buildUrl(path: string, query: Record<string, QueryValue> | undefined): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === '') continue
    params.set(key, String(value))
  }
  const search = params.toString()
  return search === '' ? `${BASE}${path}` : `${BASE}${path}?${search}`
}

async function problemFrom(response: Response): Promise<Problem> {
  const contentType = response.headers.get('Content-Type') ?? ''
  if (contentType.includes('problem+json') || contentType.includes('json')) {
    try {
      const parsed: unknown = await response.json()
      if (typeof parsed === 'object' && parsed !== null && 'title' in parsed) {
        const problem = parsed as Partial<Problem>
        return {
          type: problem.type ?? 'about:blank',
          title: problem.title ?? response.statusText,
          status: problem.status ?? response.status,
          ...(problem.detail === undefined ? {} : { detail: problem.detail }),
          ...(problem.instance === undefined ? {} : { instance: problem.instance }),
        }
      }
    } catch {
      // Fall through to the synthesised problem below.
    }
  }
  return {
    type: 'about:blank',
    title: response.statusText === '' ? 'Request failed' : response.statusText,
    status: response.status,
  }
}

/**
 * Perform one API request.
 *
 * Inputs: method, contract path (without `/api/v1`), and optional query, body, and headers.
 * Returns the decoded JSON body, or `undefined` typed as `T` for a 204.
 *
 * Failure modes: always {@link ProblemError}. A 401 additionally routes to the login view — a
 * session expires by sitting still, and the alternative is a page of empty panels. The login call
 * itself is exempt, because a wrong token must show its own message (T058).
 */
export async function request<T>(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (UNSAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf !== null) headers[CSRF_HEADER] = csrf
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      // The session cookie is the normal credential; same-origin keeps it attached without CORS.
      credentials: 'same-origin',
      ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
    })
  } catch (error) {
    throw new ProblemError({
      type: 'about:blank',
      title: 'Cannot reach the server',
      status: 0,
      detail: error instanceof Error ? error.message : String(error),
    })
  }

  if (!response.ok) {
    const problem = await problemFrom(response)
    if (problem.status === 401 && path !== LOGIN_PATH) {
      void router.push({ name: 'login', query: { next: router.currentRoute.value.fullPath } })
    }
    throw new ProblemError(problem)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/**
 * A cursor walk over one keyset-paginated collection.
 *
 * Call {@link Pager.next} repeatedly; each call feeds the previous page's `next_cursor` back to the
 * API. `done` is true once the API stops handing one out. There is no page number, because there is
 * no offset parameter to build one from (FR-030).
 */
export interface Pager<T> {
  next(): Promise<{ items: T[]; done: boolean }>
}

export function pager<T>(path: string, query: Record<string, QueryValue> = {}): Pager<T> {
  let cursor: string | null = null
  let done = false
  return {
    async next() {
      if (done) return { items: [], done: true }
      const page = await request<Page<T>>('GET', path, {
        query: { ...query, ...(cursor === null ? {} : { cursor }) },
      })
      cursor = page.next_cursor
      done = cursor === null || cursor === undefined
      return { items: page.items, done }
    },
  }
}

/**
 * Exchange the API token for a session cookie.
 *
 * Inputs: the raw token, sent once as a bearer header. It is never placed in a URL, in a query
 * string, or in rendered markup (FR-032) — the browser keeps only the HttpOnly cookie the server
 * sets in response.
 *
 * Failure modes: {@link ProblemError} 401 when the token does not match.
 */
export function login(token: string): Promise<void> {
  return request<void>('POST', LOGIN_PATH, { headers: { Authorization: `Bearer ${token}` } })
}

export function entries(query: EntryQuery = {}): Pager<Entry> {
  return pager<Entry>('/entries', { ...query })
}

export function work(id: string): Promise<WorkDetail> {
  return request<WorkDetail>('GET', `/works/${encodeURIComponent(id)}`)
}

export function creators(): Pager<Creator> {
  return pager<Creator>('/creators')
}

export function creator(id: string): Promise<CreatorDetail> {
  return request<CreatorDetail>('GET', `/creators/${encodeURIComponent(id)}`)
}

export function providers(): Promise<Provider[]> {
  return request<Provider[]>('GET', '/providers')
}

/**
 * Enable or disable a provider.
 *
 * The contract names the capability (T056, `GET /providers` returns `enabled`) but does not spell
 * out the write. `PATCH /providers/{id}` with the changed field is the only shape consistent with
 * the rest of the document, so that is what this sends.
 */
export function setProviderEnabled(id: string, enabled: boolean): Promise<Provider> {
  return request<Provider>('POST', `/providers/${encodeURIComponent(id)}/${enabled ? 'enable' : 'disable'}`)
}

export function syncProvider(
  id: string,
  mode: 'incremental' | 'full' = 'incremental',
): Promise<{ lineage_id?: string }> {
  return request<{ lineage_id?: string }>('POST', `/providers/${encodeURIComponent(id)}/sync`, {
    body: { mode },
  })
}

export function checkProvider(id: string): Promise<CheckResult> {
  return request<CheckResult>('POST', `/providers/${encodeURIComponent(id)}/check`)
}

export function health(): Promise<Health> {
  return request<Health>('GET', '/health')
}

export function providerRuns(id: string): Pager<SyncRun> {
  return pager<SyncRun>(`/providers/${encodeURIComponent(id)}/runs`)
}

export function ingestFailures(): Pager<IngestFailure> {
  return pager<IngestFailure>('/ingest-failures')
}

export function replayFailure(id: number): Promise<{ replayed: boolean }> {
  return request<{ replayed: boolean }>('POST', `/ingest-failures/${id}/replay`)
}

export function resolutionQueue(query: { subject?: 'work' | 'creator' } = {}): Pager<ResolutionItem> {
  return pager<ResolutionItem>('/resolution-queue', query)
}

export function decideResolution(
  id: number,
  decision: 'linked' | 'created' | 'split' | 'ignored',
  targetId?: string,
): Promise<MergeLogEntry> {
  return request<MergeLogEntry>('POST', `/resolution-queue/${id}/decide`, {
    body: { decision, ...(targetId === undefined ? {} : { target_id: targetId }) },
  })
}

export function mergeWork(id: string, loserIds: string[]): Promise<MergeLogEntry> {
  return request<MergeLogEntry>('POST', `/works/${encodeURIComponent(id)}/merge`, {
    body: { loser_ids: loserIds },
  })
}

export function mergeCreator(id: string, loserIds: string[]): Promise<MergeLogEntry> {
  return request<MergeLogEntry>('POST', `/creators/${encodeURIComponent(id)}/merge`, {
    body: { loser_ids: loserIds },
  })
}

export function splitCreator(
  id: string,
  creditIds: number[],
  newName?: string,
): Promise<MergeLogEntry> {
  return request<MergeLogEntry>('POST', `/creators/${encodeURIComponent(id)}/split`, {
    body: { credit_ids: creditIds, ...(newName === undefined ? {} : { new_name: newName }) },
  })
}

export function undoMerge(id: number): Promise<{ undone: boolean }> {
  return request<{ undone: boolean }>('POST', `/merge-log/${id}/undo`)
}
