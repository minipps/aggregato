<script setup lang="ts">
/** Show installed providers, their status, settings, and operator actions. */

import { computed, onUnmounted, ref } from 'vue'

import {
  checkProvider,
  importProviderFile,
  latestProviderRun,
  providerConfigSchema,
  providers,
  setProviderEnabled,
  syncProvider,
  toProblem,
  updateProviderConfig,
} from '@/api/client'
import { readonlyAccess } from '@/api/session'
import { useSyncStream } from '@/api/syncStream'
import type { JsonSchema, Problem } from '@/api/types'
import { useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'
import ProviderStatus from '@/components/ProviderStatus.vue'
import SchemaForm from '@/components/SchemaForm.vue'
import SyncProgress from '@/components/SyncProgress.vue'

const list = useRequest(providers)
const syncStream = useSyncStream()
const CHECK_POLL_ATTEMPTS = 60
const CHECK_POLL_INTERVAL_MS = 5000
const checkTimers = new Map<string, ReturnType<typeof setTimeout>>()
const checkGenerations = new Map<string, number>()
let stopped = false

/** Sort enabled providers first while preserving server order within each group. */
const sorted = computed(() =>
  [...(list.data.value ?? [])].sort(
    (a, b) => Number(b.enabled) - Number(a.enabled),
  ),
)

/** Keep action feedback associated with the provider card that triggered it. */
const busy = ref('')
const notice = ref<{ id: string; text: string } | undefined>(undefined)
const failure = ref<{ id: string; problem: Problem } | undefined>(undefined)
const importFile = ref<File | undefined>(undefined)
const configSchemas = ref<Record<string, JsonSchema | undefined>>({})
const configSchemaFailure = ref<{ id: string; problem: Problem } | undefined>(undefined)
type ConfigValues = Record<string, string | number | boolean | null>
const configValues = ref<Record<string, ConfigValues>>({})

function liveRun(id: string) {
  return syncStream.snapshot.value?.runs.find((run) => run.provider_id === id && run.status === 'running')
}

function liveProvider(id: string) {
  return syncStream.snapshot.value?.providers.find((provider) => provider.id === id)
}

function cancelCheckPoll(id: string): number {
  const timer = checkTimers.get(id)
  if (timer !== undefined) window.clearTimeout(timer)
  checkTimers.delete(id)
  const generation = (checkGenerations.get(id) ?? 0) + 1
  checkGenerations.set(id, generation)
  return generation
}

function pollCheck(id: string, lineageId: string, attempt = 0, generation = checkGenerations.get(id)): void {
  if (stopped || generation === undefined || generation !== checkGenerations.get(id)) return
  if (attempt >= CHECK_POLL_ATTEMPTS) {
    if (notice.value?.id === id && notice.value.text === 'Provider check queued.') {
      notice.value = { id, text: 'Provider check is still pending; refresh the page to see its result.' }
    }
    return
  }

  const timer = window.setTimeout(async () => {
    if (checkTimers.get(id) === timer) checkTimers.delete(id)
    if (stopped || generation !== checkGenerations.get(id)) return
    try {
      const updated = await providers()
      if (stopped || generation !== checkGenerations.get(id)) return
      list.data.value = updated
      list.error.value = undefined
      const latest = updated.find((provider) => provider.id === id)?.last_check
      if (latest?.lineage_id === lineageId && latest.status !== 'pending') {
        if (notice.value?.id === id && notice.value.text === 'Provider check queued.') {
          notice.value = { id, text: `Provider check ${latest.status}.` }
        }
        return
      }
    } catch {
      // Retry transient list errors within the same five-minute polling window.
    }
    if (!stopped && generation === checkGenerations.get(id)) {
      pollCheck(id, lineageId, attempt + 1, generation)
    }
  }, CHECK_POLL_INTERVAL_MS)
  checkTimers.set(id, timer)
}

function formatSetting(value: unknown): string {
  if (value === null) return 'None'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

async function act(id: string, label: string, action: () => Promise<string>): Promise<void> {
  if (busy.value) return
  busy.value = id
  notice.value = { id, text: `${label}…` }
  failure.value = undefined
  try {
    notice.value = { id, text: await action() }
  } catch (caught) {
    notice.value = undefined
    failure.value = { id, problem: toProblem(caught) }
  } finally {
    busy.value = ''
  }
}

function toggle(id: string, enabled: boolean): Promise<void> {
  return act(id, enabled ? 'Enabling' : 'Disabling', async () => {
    await setProviderEnabled(id, enabled)
    await list.reload()
    return enabled ? 'Enabled.' : 'Disabled.'
  })
}

function sync(id: string): Promise<void> {
  return act(id, 'Queueing a sync', async () => {
    await syncProvider(id)
    await list.reload()
    return 'Sync queued.'
  })
}

function check(id: string): Promise<void> {
  return act(id, 'Queueing a provider check', async () => {
    const generation = cancelCheckPoll(id)
    const { lineage_id } = await checkProvider(id)
    await list.reload()
    const latest = list.data.value?.find((provider) => provider.id === id)?.last_check
    if (latest?.lineage_id === lineage_id && latest.status !== 'pending') {
      return `Provider check ${latest.status}.`
    }
    pollCheck(id, lineage_id, 0, generation)
    return 'Provider check queued.'
  })
}

/**
 * Fetch all history the provider currently exposes, ignoring the saved cursor. Stored payload replay
 * runs separately when a normalization schema version changes.
 */
function fullResync(id: string): Promise<void> {
  return act(id, 'Queueing a full resync', async () => {
    await syncProvider(id, 'full')
    await list.reload()
    return [
      'Full sync queued. It fetches all history the provider currently exposes, ignoring the saved cursor.',
      'Stored payload replay is handled automatically when the normalization schema changes.',
    ].join(' ')
  })
}

function showLatestRun(id: string): Promise<void> {
  return act(id, 'Loading latest run', async () => {
    const result = await latestProviderRun(id)
    if (result.status === 'success') return 'Latest run completed successfully.'
    const status = result.status ?? 'not yet known'
    const errorClass = result.error_class ?? 'no error class'
    return `Latest run ${status} (${errorClass}): ${result.detail ?? 'no detail given'}`
  })
}

function chooseImport(event: Event): void {
  importFile.value = (event.target as HTMLInputElement).files?.[0]
}

function importExport(id: string): Promise<void> {
  return act(id, 'Uploading export', async () => {
    if (!importFile.value) throw new Error('Choose an export file first.')
    await importProviderFile(id, importFile.value)
    importFile.value = undefined
    return 'Export uploaded; import queued.'
  })
}

async function showConfigSchema(id: string): Promise<void> {
  if (busy.value) return
  configSchemaFailure.value = undefined
  if (configSchemas.value[id]) {
    configSchemas.value = { ...configSchemas.value, [id]: undefined }
    return
  }
  busy.value = id
  try {
    configSchemas.value = { ...configSchemas.value, [id]: await providerConfigSchema(id) }
    configValues.value = { ...configValues.value, [id]: {} }
  } catch (caught) {
    configSchemaFailure.value = { id, problem: toProblem(caught) }
  } finally {
    busy.value = ''
  }
}

function saveConfiguration(id: string): Promise<void> {
  return act(id, 'Saving configuration', async () => {
    await updateProviderConfig(id, configValues.value[id] ?? {})
    await list.reload()
    return 'Configuration saved. An enabled provider with a suspended schedule may resume automatically.'
  })
}

onUnmounted(() => {
  stopped = true
  for (const timer of checkTimers.values()) window.clearTimeout(timer)
  checkTimers.clear()
  checkGenerations.clear()
})
</script>

<template>
  <section>
    <div class="bento">
      <section class="card card--accent span-2">
        <div class="card__head">
          <span class="card__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M4 7h10M4 17h6m4-10a3 3 0 1 0 6 0 3 3 0 0 0-6 0Zm-4 10a3 3 0 1 0 6 0 3 3 0 0 0-6 0Z" />
            </svg>
          </span>
          <h1>Providers</h1>
        </div>
        <p>Every platform this archive reads from, how it acquires data, and what it needs from you.</p>
      </section>

      <section class="card" aria-labelledby="installed-label">
        <p id="installed-label" class="metric-label">Installed</p>
        <p class="metric">{{ (list.data.value ?? []).length }}</p>
      </section>

      <section class="card" aria-labelledby="enabled-label">
        <p id="enabled-label" class="metric-label">Enabled</p>
        <p class="metric">{{ (list.data.value ?? []).filter((provider) => provider.enabled).length }}</p>
      </section>
    </div>

    <LoadingState v-if="list.loading.value" label="Loading providers…" />
    <ErrorState
      v-else-if="list.error.value"
      :problem="list.error.value"
      retryable
      @retry="list.reload()"
    />
    <EmptyState
      v-else-if="(list.data.value ?? []).length === 0"
      title="No providers installed"
      detail="Providers ship with the release; none is configured yet."
    />

    <ul v-else class="bento">
      <!-- The first provider spans the row; remaining cards use half-width columns. -->
      <li
        v-for="(provider, index) in sorted"
        :key="provider.id"
        :class="['card', index === 0 ? 'span-4' : 'span-2']"
      >
        <div class="card__head">
          <h2>{{ provider.name }}</h2>
          <!-- Drop-in providers are marked as unreviewed. -->
          <span v-if="!provider.reviewed" class="badge badge--warn">unreviewed</span>
          <ProviderStatus :status="liveProvider(provider.id)?.status ?? provider.status" />
        </div>

        <p class="muted">
          {{ provider.acquisition }} · {{ provider.enabled ? 'enabled' : 'disabled' }}
          <template v-if="provider.consecutive_failures">
            · {{ provider.consecutive_failures }} consecutive failures
          </template>
        </p>

        <div v-if="!provider.reviewed" class="note note--warn" role="alert">
          This is a local drop-in provider. Review its code and configuration before enabling it.
        </div>
        <div v-if="provider.acquisition === 'scrape'" class="note note--warn" role="note">
          Scraping can trigger rate limits or blocks. Use only an account you control; Aggregato does not bypass CAPTCHA.
        </div>

        <!-- Keep the primary action before its error text. -->
        <div v-if="provider.last_error?.action_required" class="note note--danger" role="alert">
          <p class="note__title">Action required</p>
          <p>{{ provider.last_error.action_required }}</p>
          <p v-if="provider.last_error.message">
            {{ provider.last_error.error_class }}: {{ provider.last_error.message }}
          </p>
        </div>
        <p v-else-if="provider.last_error?.message" class="muted">
          Last error ({{ provider.last_error.error_class }}): {{ provider.last_error.message }}
        </p>

        <section
          v-if="provider.last_check"
          class="subsection"
          :aria-label="`${provider.name} latest provider check`"
        >
          <h3>Latest provider check</h3>
          <dl class="pairs">
            <dt>Status</dt>
            <dd><span class="badge">{{ provider.last_check.status }}</span></dd>
            <dt>Lineage</dt>
            <dd class="mono">{{ provider.last_check.lineage_id }}</dd>
            <dt>Requested</dt>
            <dd>
              <LoggedAt
                v-if="provider.last_check.requested_at"
                :at="provider.last_check.requested_at"
                precision="exact"
              />
              <span v-else class="muted">Not recorded</span>
            </dd>
            <dt>Completed</dt>
            <dd>
              <LoggedAt
                v-if="provider.last_check.completed_at"
                :at="provider.last_check.completed_at"
                precision="exact"
              />
              <span v-else class="muted">Pending</span>
            </dd>
            <dt>Detail</dt>
            <dd>{{ provider.last_check.detail ?? '—' }}</dd>
            <dt>Error class</dt>
            <dd>{{ provider.last_check.error_class ?? '—' }}</dd>
          </dl>
        </section>

        <section
          v-if="liveRun(provider.id) || liveProvider(provider.id)?.requested_mode"
          class="subsection"
          :aria-label="`${provider.name} current sync progress`"
        >
          <h3>Current sync</h3>
          <SyncProgress v-if="liveRun(provider.id)" :run="liveRun(provider.id)!" />
          <p v-else class="muted" role="status">
            {{ liveProvider(provider.id)?.requested_mode }} sync queued; waiting for the scheduler…
          </p>
        </section>

        <dl class="pairs">
          <dt>Last success</dt>
          <dd>
            <LoggedAt v-if="provider.last_success_at" :at="provider.last_success_at" precision="exact" />
            <span v-else class="muted">Never</span>
          </dd>
          <dt>Next run</dt>
          <dd>
            <LoggedAt v-if="provider.next_run_at" :at="provider.next_run_at" precision="exact" />
            <span v-else class="muted">Not scheduled</span>
          </dd>
          <dt>Media types</dt>
          <dd>{{ provider.media_types.join(', ') || '—' }}</dd>
          <dt>Capabilities</dt>
          <dd>{{ provider.capabilities.join(', ') || '—' }}</dd>
        </dl>

        <section
          v-if="Object.keys(provider.current_settings ?? {}).length"
          class="subsection"
          :aria-label="`${provider.name} current settings`"
        >
          <h3>Current settings</h3>
          <dl class="pairs">
            <template v-for="(value, setting) in provider.current_settings" :key="setting">
              <dt>{{ setting }}</dt>
              <dd class="mono">{{ formatSetting(value) }}</dd>
            </template>
          </dl>
          <p class="muted">Passwords, API keys, and other sensitive settings are not shown.</p>
        </section>

        <!-- File-pinned settings are disabled and explained below. -->
        <fieldset v-if="provider.file_pinned_settings?.length" disabled class="pinned">
          <legend>Fixed by the configuration file</legend>
          <p class="muted">
            These are set in the config file on disk, which takes precedence over anything edited
            here, so they cannot be changed from the UI.
          </p>
          <ul class="plain">
            <li v-for="setting in provider.file_pinned_settings" :key="setting" class="mono">
              {{ setting }}
            </li>
          </ul>
        </fieldset>

        <p v-if="readonlyAccess === false" class="actions">
          <button type="button" :disabled="busy !== ''" @click="toggle(provider.id, !provider.enabled)">
            {{ provider.enabled ? 'Disable' : 'Enable' }}
          </button>
          <button
            type="button"
            :disabled="busy !== '' || provider.last_check?.status === 'pending'"
            @click="check(provider.id)"
          >
            Check provider
          </button>
          <button type="button" :disabled="busy !== '' || !provider.enabled" @click="sync(provider.id)">
            Sync now
          </button>
          <button
            type="button"
            :disabled="busy !== '' || !provider.enabled"
            :title="`Fetch all history currently exposed by ${provider.name}, ignoring the saved cursor`"
            @click="fullResync(provider.id)"
          >
            Full resync
          </button>
          <button type="button" :disabled="busy !== ''" @click="showConfigSchema(provider.id)">
            {{ configSchemas[provider.id] ? 'Hide configuration' : 'Configure provider' }}
          </button>
        </p>

        <p class="actions">
          <button type="button" :disabled="busy !== ''" @click="showLatestRun(provider.id)">
            Show latest sync run
          </button>
        </p>

        <section v-if="configSchemas[provider.id]" class="subsection" :aria-label="`${provider.name} configuration`">
          <h3>Configuration fields</h3>
          <SchemaForm
            v-model="configValues[provider.id]"
            :schema="configSchemas[provider.id]!"
            :disabled="busy !== '' || readonlyAccess !== false || Boolean(provider.file_pinned_settings?.length)"
            @submit="saveConfiguration(provider.id)"
          >
            <button
              v-if="readonlyAccess === false && !provider.file_pinned_settings?.length"
              :disabled="busy !== ''"
              type="submit"
            >Save configuration</button>
          </SchemaForm>
          <p v-if="provider.file_pinned_settings?.length" class="muted">
            This provider is configured by the mounted file, so web edits are unavailable.
          </p>
          <template v-else>
            <p class="muted">
              Existing values, including credentials, are never shown. Complete required fields to
              replace the saved configuration.
            </p>
          </template>
        </section>
        <ErrorState
          v-if="configSchemaFailure && configSchemaFailure.id === provider.id"
          :problem="configSchemaFailure.problem"
        />

        <div v-if="provider.capabilities.includes('file_import') && readonlyAccess === false" class="import-export subsection">
          <label :for="`import-${provider.id}`">Import personal export</label>
          <input
            :id="`import-${provider.id}`"
            type="file"
            accept=".csv,.rss,.xml,text/csv,application/rss+xml,application/xml,text/xml"
            :disabled="busy !== '' || !provider.enabled"
            @change="chooseImport"
          >
          <button
            type="button"
            :disabled="busy !== '' || !provider.enabled || !importFile"
            @click="importExport(provider.id)"
          >
            Upload and import
          </button>
          <p class="muted">Exports stay local to this archive and are processed by the sync worker.</p>
        </div>

        <!-- Always present, so the live region announces rather than being inserted mid-update. -->
        <p class="notice" role="status" aria-live="polite">
          {{ notice && notice.id === provider.id ? notice.text : '' }}
        </p>
        <ErrorState
          v-if="failure && failure.id === provider.id"
          :problem="failure.problem"
        />
      </li>
    </ul>
  </section>
</template>

<style scoped>
/* Layout only: surfaces, tints, and radii all come from the shared card and note styles, so a
   provider tile looks like every other Bento tile. */
.pairs {
  grid-template-columns: max-content minmax(0, 1fr);
}

.pinned {
  margin: 0;
}

.pinned p {
  margin: 0 0 var(--space-2);
}

.notice {
  min-height: 1.5em;
  color: var(--text-muted);
}

.import-export {
  display: grid;
  gap: var(--space-2);
}

.subsection {
  display: grid;
  gap: var(--space-2);
}
</style>
