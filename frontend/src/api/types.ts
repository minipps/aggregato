/**
 * Hand-written mirror of `docs/contracts/openapi.yaml`.
 *
 * Hand-written rather than generated:  makes the contract the only interface the UI may use,
 * and a codegen step would be a build dependency for types that change once per contract revision.
 * Only the schemas  actually renders are declared here.
 */

export type MediaType =
  | 'film'
  | 'tv'
  | 'book'
  | 'comic'
  | 'manga'
  | 'anime'
  | 'album'
  | 'track'
  | 'game'
  | 'other'

export type MediaFamily = 'screen' | 'print' | 'audio' | 'interactive' | 'other'

export type EntryKind = 'watch' | 'rewatch' | 'listen' | 'read' | 'finish' | 'progress' | 'drop'

export type Role =
  | 'author'
  | 'illustrator'
  | 'translator'
  | 'editor'
  | 'director'
  | 'writer'
  | 'composer'
  | 'performer'
  | 'featured_performer'
  | 'voice'
  | 'narrator'
  | 'studio'
  | 'publisher'
  | 'developer'
  | 'other'

export type Confidence = 'asserted' | 'matched' | 'manual'

export type ErrorClass =
  | 'auth'
  | 'rate_limit'
  | 'transport'
  | 'parse'
  | 'structure_changed'
  | 'blocked'
  | 'internal'

export type ProviderStatus = 'disabled' | 'idle' | 'syncing' | 'degraded' | 'misconfigured'
export type RunStatus = 'running' | 'success' | 'partial' | 'failed'
export type RunPhase =
  | 'starting'
  | 'checking'
  | 'replaying'
  | 'fetching'
  | 'ingesting'
  | 'finalizing'
  | 'finished'
  | 'failed'
export type ProviderCheckStatus = 'pending' | 'success' | 'failure'

export type Acquisition = 'api' | 'feed' | 'export' | 'scrape'

export type ProviderCapability =
  | 'poll'
  | 'backfill'
  | 'file_import'
  | 'reports_deletes'
  | 'has_ratings'
  | 'has_reviews'
  | 'has_credits'
  | 'scrapes'
  | 'push'

/** Precision of a logged date. Nothing may render more precision than this states . */
export type LoggedPrecision = 'exact' | 'day' | 'month' | 'year' | 'unknown'

/** RFC 9457 problem detail — the one error shape the whole UI renders (UI consistency guidance). */
export interface Problem {
  type: string
  title: string
  status: number
  detail?: string
  instance?: string
}

/** Keyset page. `next_cursor` is the only way forward; the API has no offset . */
export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export interface SubjectRef {
  season?: number
  episode?: number
  track?: number
  disc?: number
  chapter?: number
  volume?: number
}

/** Raw and normalized together; normalized is not a cross-platform claim . */
export interface Rating {
  raw: number | null
  scale_id: string | null
  normalized: number | null
}

export interface ExternalId {
  namespace: string
  value: string
  source: string
  confidence: Confidence
}

export interface Work {
  id: string
  media_type: MediaType
  media_family: MediaFamily
  title: string
  original_title?: string | null
  release_year?: number | null
  parent_work_id?: string | null
  sequence_number?: number | null
  image?: string | null
  entry_count?: number
  providers?: string[]
}

export interface Entry {
  id: number
  work_id: string
  work?: Work
  provider_id: string
  kind: EntryKind
  logged_at: string
  logged_precision: LoggedPrecision
  subject_ref?: SubjectRef | null
  progress?: { value?: number; unit?: string } | null
  ingested_at?: string
  deleted_at?: string | null
}

export interface Opinion {
  id: number
  work_id: string
  provider_id: string
  rating?: Rating | null
  subject_ref?: SubjectRef | null
  is_liked?: boolean | null
  review_text?: string | null
  review_format?: 'plain' | 'markdown' | 'html' | null
  contains_spoilers?: boolean | null
  authored_at?: string | null
}

export interface Credit {
  id: number
  creator_id: string
  creator_name?: string
  role: Role
  role_raw?: string | null
  credited_as?: string | null
  position: number
  source: string
  link_confidence: Confidence
}

export interface Creator {
  id: string
  kind: 'person' | 'group' | 'studio' | 'imprint' | 'unknown'
  name: string
  image?: string | null
  families?: MediaFamily[]
  credit_count?: number
  logged_count?: number
}

export interface CreatorDetail extends Creator {
  aliases?: { name: string; media_family: MediaFamily; kind: string }[]
  external_ids?: ExternalId[]
  credits_by_role?: Record<string, Credit[]>
}

export interface ResolutionItem {
  id: number
  subject: 'work' | 'creator'
  provider_id: string
  payload_ref?: number | null
  candidates: { id: string; label?: string; name?: string; score?: number; reason: string }[]
  proposed: Record<string, unknown>
  suggestion_kind?: string | null
  created_at: string
}

export interface MergeLogEntry {
  id: number
  subject: 'work' | 'creator'
  operation: 'merge' | 'split'
  winner_id: string
  loser_ids: string[]
  performed_at: string
  undo_url: string
}

export interface WorkDetail extends Work {
  external_ids?: ExternalId[]
  credits?: Credit[]
  entries?: Entry[]
  opinions?: Opinion[]
  parent?: Work
  siblings?: Work[]
}

export interface ProviderError {
  error_class?: ErrorClass
  message?: string
  action_required?: string
}

export interface LastCheck {
  status: ProviderCheckStatus
  lineage_id: string
  requested_at?: string | null
  completed_at?: string | null
  detail?: string | null
  error_class?: ErrorClass | null
}

export interface QueuedOperation {
  lineage_id: string
}

export interface Provider {
  id: string
  name: string
  enabled: boolean
  status: ProviderStatus
  acquisition: Acquisition
  reviewed: boolean
  capabilities: ProviderCapability[]
  media_types: MediaType[]
  poll_interval_seconds?: number
  next_run_at?: string | null
  last_success_at?: string | null
  consecutive_failures?: number
  last_error?: ProviderError | null
  last_check: LastCheck | null
  file_pinned_settings?: string[]
  /** Configured values that the API has classified as safe to show; credentials are omitted. */
  current_settings?: Record<string, unknown>
}

export interface LastRun {
  status?: RunStatus
  error_class?: ErrorClass
  detail?: string
}

/** The useful, renderable subset of the JSON Schema returned for provider configuration. */
export interface JsonSchema {
  title?: string
  description?: string
  type?: string
  properties?: Record<string, JsonSchema>
  required?: string[]
  enum?: Array<string | number>
  default?: string | number | boolean | null
  format?: string
  writeOnly?: boolean
  anyOf?: JsonSchema[]
}

export interface ProviderHealth {
  id?: string
  status?: ProviderStatus
  last_success_at?: string | null
  consecutive_failures?: number
}

export interface ArchiveSettings {
  raw_payload_retention_days: number
  success_run_retention_days: number
  failure_run_retention_days: number
  image_cache_enabled: boolean
  storage: { raw_payload_bytes: number; image_cache_bytes: number; database_bytes: number }
}

export type StatsPeriod = 'all' | 'year' | 'month' | 'week' | 'day'

export interface StatsSummary {
  period: StatsPeriod
  total_entries: number
  by_media_type: Record<string, number>
  by_provider: Record<string, number>
  by_period: { period: string; count: number }[]
}

export interface TopStat {
  id: string
  label: string
  count: number
  media_type?: MediaType | null
}

export interface TopStats {
  group: 'work' | 'creator'
  items: TopStat[]
}

/** `GET /auth/session`: how this browser is authenticated, and whether it may write. */
export interface Session {
  via: 'bearer' | 'cookie' | 'public'
  readonly: boolean
}

export interface Health {
  status: 'ok' | 'degraded'
  providers: ProviderHealth[]
}

export interface SyncRun {
  id: number
  provider_id: string
  lineage_id: string
  attempt: number
  mode: 'incremental' | 'full' | 'import' | 'check'
  status: RunStatus
  phase: RunPhase
  started_at: string
  finished_at?: string | null
  updated_at: string
  items_seen: number
  items_written: number
  items_failed: number
  progress_total?: number | null
  progress_percent?: number | null
  checkpoint_count: number
  last_checkpoint_at?: string | null
  cursor_before?: Record<string, unknown> | null
  cursor_after?: Record<string, unknown> | null
  error_class?: ErrorClass | null
  error_message?: string | null
  next_retry_at?: string | null
  log_excerpt?: string | null
}

export interface SyncProviderState {
  id: string
  enabled: boolean
  status: ProviderStatus
  requested_mode?: 'incremental' | 'full' | 'import' | 'replay' | 'check' | null
  requested_lineage_id?: string | null
  next_run_at?: string | null
}

export interface SyncSnapshot {
  type: 'snapshot'
  generated_at: string
  providers: SyncProviderState[]
  runs: SyncRun[]
}

export interface IngestFailure {
  id: number
  provider_id: string
  sync_run_id: number
  native_id: string | null
  stage: 'fetch' | 'validate' | 'normalize' | 'write'
  error: string
  raw_payload: Record<string, unknown>
  created_at: string
  resolved_at?: string | null
}

/** Query parameters of `GET /entries` that the log view exposes. */
export interface EntryQuery {
  media_type?: MediaType
  media_family?: MediaFamily
  provider?: string
  kind?: EntryKind
  /** Convenience over `kind`: every kind that means the work was finished. */
  status?: 'completed'
  from?: string
  to?: string
  q?: string
  limit?: number
}
