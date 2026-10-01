/** Shared API request handling and typed endpoint calls. */

import { router } from '@/router'

import type {
  Creator,
  CreatorDetail,
  Entry,
  EntryQuery,
  Health,
  JsonSchema,
  Page,
  Problem,
  Provider,
  QueuedOperation,
  Session,
  SyncRun,
  SyncRunDiagnostics,
  SyncSnapshot,
  IngestFailure,
  LastRun,
  MergeLogEntry,
  ResolutionItem,
  WorkDetail,
  ArchiveSettings,
  StatsPeriod,
  StatsSummary,
  TopStats,
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
  responseType?: 'json' | 'blob'
  /** Extra headers. Used only by the login call, which presents the API token as a bearer. */
  headers?: Record<string, string>
}

/**
 * Error carrying an RFC 9457 problem detail for API failures.
 */
export class ProblemError extends Error {
  readonly problem: Problem

  constructor(problem: Problem) {
    super(problem.detail ?? problem.title)
    this.name = 'ProblemError'
    this.problem = problem
  }
}

/** Convert a caught value to the problem shape used by error views. */
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
  // The CSRF cookie is readable by JavaScript; the session cookie is HttpOnly.
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
 * Returns JSON, a blob when requested, or `undefined` for a 204.
 *
 * HTTP failures throw {@link ProblemError}. A 401 from any other route navigates to the login view.
 */
export async function request<T>(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
  const formData = options.body instanceof FormData
  if (options.body !== undefined && !formData) headers['Content-Type'] = 'application/json'
  if (UNSAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf !== null) headers[CSRF_HEADER] = csrf
  }

  let response: Response
  try {
    const body = options.body === undefined
      ? undefined
      : formData
        ? options.body as FormData
        : JSON.stringify(options.body)
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      // The session cookie is the normal credential; same-origin keeps it attached without CORS.
      credentials: 'same-origin',
      ...(body === undefined ? {} : { body }),
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
  if (options.responseType === 'blob') return await response.blob() as T
  return (await response.json()) as T
}

/**
 * A cursor walk over one keyset-paginated collection.
 *
 * Call {@link Pager.next} repeatedly; each call feeds the previous page's `next_cursor` back to the
 * API. `done` is true once the API stops handing one out.
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

/** Exchange an API token for a session cookie. */
export function login(token: string): Promise<void> {
  return request<void>('POST', LOGIN_PATH, { headers: { Authorization: `Bearer ${token}` } })
}

/** How this browser is authenticated. The SPA reads `readonly` to hide what it cannot do. */
export function session(): Promise<Session> {
  return request<Session>('GET', LOGIN_PATH)
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

/** JSON Schema for the configuration model declared by one provider. */
export function providerConfigSchema(id: string): Promise<JsonSchema> {
  return request<JsonSchema>('GET', `/providers/${encodeURIComponent(id)}/config-schema`)
}

/** Save provider settings validated by its declared schema. */
export function updateProviderConfig(
  id: string,
  settings: Record<string, string | number | boolean | null>,
): Promise<Provider> {
  return request<Provider>('PUT', `/providers/${encodeURIComponent(id)}/config`, { body: settings })
}

/** Enable or disable a provider through its action endpoint. */
export function setProviderEnabled(id: string, enabled: boolean): Promise<Provider> {
  return request<Provider>('POST', `/providers/${encodeURIComponent(id)}/${enabled ? 'enable' : 'disable'}`)
}

export function syncProvider(
  id: string,
  mode: 'incremental' | 'full' = 'incremental',
): Promise<QueuedOperation> {
  return request<QueuedOperation>('POST', `/providers/${encodeURIComponent(id)}/sync`, {
    body: { mode },
  })
}

/** Queue an isolated provider credential diagnostic; the worker performs it asynchronously. */
export function checkProvider(id: string): Promise<QueuedOperation> {
  return request<QueuedOperation>('POST', `/providers/${encodeURIComponent(id)}/check`)
}

/** Upload a personal export without assigning a JSON content type to multipart form data. */
export async function importProviderFile(
  id: string,
  file: File,
): Promise<QueuedOperation> {
  const data = new FormData()
  data.append('file', file)
  return request<QueuedOperation>('POST', `/providers/${encodeURIComponent(id)}/import`, { body: data })
}

export function latestProviderRun(id: string): Promise<LastRun> {
  return request<LastRun>('GET', `/providers/${encodeURIComponent(id)}/last-run`)
}

export function health(): Promise<Health> {
  return request<Health>('GET', '/health')
}

/** Archive activity counts; sub-unit activity stays excluded unless the caller explicitly opts in. */
export function statsSummary(
  period: StatsPeriod = 'all',
  includeSubunits = false,
): Promise<StatsSummary> {
  return request<StatsSummary>('GET', '/stats/summary', {
    query: { period, include_subunits: includeSubunits },
  })
}

export function topStats(
  group: 'work' | 'creator',
  includeSubunits = false,
): Promise<TopStats> {
  return request<TopStats>('GET', '/stats/top', {
    query: { group, include_subunits: includeSubunits },
  })
}

export function providerRuns(id: string): Pager<SyncRun> {
  return pager<SyncRun>(`/providers/${encodeURIComponent(id)}/runs`)
}

export function syncRunDiagnostics(providerId: string, runId: number): Promise<SyncRunDiagnostics> {
  return request<SyncRunDiagnostics>('GET', `/providers/${encodeURIComponent(providerId)}/runs/${runId}/diagnostics`)
}

/** Read the same durable queue/latest-run snapshot that the sync websocket streams. */
export function syncStatus(query: { provider_id?: string; lineage_id?: string } = {}): Promise<SyncSnapshot> {
  return request<SyncSnapshot>('GET', '/sync/status', { query })
}

export function ingestFailures(): Pager<IngestFailure> {
  return pager<IngestFailure>('/ingest-failures')
}

export function replayFailure(id: number): Promise<{ replayed: boolean; queued?: boolean; job_id?: number }> {
  return request<{ replayed: boolean; queued?: boolean; job_id?: number }>('POST', `/ingest-failures/${id}/replay`)
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

export function archiveSettings(): Promise<ArchiveSettings> {
  return request<ArchiveSettings>('GET', '/settings')
}

export function updateArchiveSettings(
  body: Partial<Omit<ArchiveSettings, 'storage'>>,
): Promise<ArchiveSettings> {
  return request<ArchiveSettings>('PATCH', '/settings', { body })
}

/** Download the streaming archive without exposing the API token to page source or URLs. */
export async function downloadArchive(): Promise<void> {
  const archive = await request<Blob>('GET', '/export', { responseType: 'blob' })
  const href = URL.createObjectURL(archive)
  const link = document.createElement('a')
  link.href = href
  link.download = 'aggregato-export.zip'
  link.click()
  URL.revokeObjectURL(href)
}
