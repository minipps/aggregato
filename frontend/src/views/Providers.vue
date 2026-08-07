<script setup lang="ts">
/**
 * Providers: what is installed, how it acquires data, whether it is healthy, and what the operator
 * must do about it.
 *
 * Three things here are requirements rather than presentation choices:
 *   * `reviewed: false` is labelled **unreviewed**  — a drop-in development provider must
 *     never look like a shipped one.
 *   * `last_error.action_required` is the loudest thing on a failing card : the point of the
 *     screen is the next action, not the stack of error text.
 *   * settings listed in `file_pinned_settings` are shown as uneditable, with the reason  —
 *     otherwise the UI silently discards edits the config file overrides.
 */

import { computed, ref } from 'vue'

import {
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
import type { JsonSchema, Problem } from '@/api/types'
import { useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'
import ProviderStatus from '@/components/ProviderStatus.vue'
import SchemaForm from '@/components/SchemaForm.vue'

const list = useRequest(providers)

/** Enabled providers first — those are the ones actually feeding the archive. Array.prototype.sort
 *  is stable, so within each group the server's order is preserved. */
const sorted = computed(() =>
  [...(list.data.value ?? [])].sort(
    (a, b) => Number(b.enabled) - Number(a.enabled),
  ),
)

/**
 * Action feedback, attached to the provider it belongs to: one provider's failed action reports on
 * that card only and never blanks the list (UI consistency guidance). Buttons are disabled while an action
 * runs, so at most one is ever in flight.
 */
const busy = ref('')
const notice = ref<{ id: string; text: string } | undefined>(undefined)
const failure = ref<{ id: string; problem: Problem } | undefined>(undefined)
const importFile = ref<File | undefined>(undefined)
const configSchemas = ref<Record<string, JsonSchema | undefined>>({})
const configSchemaFailure = ref<{ id: string; problem: Problem } | undefined>(undefined)
type ConfigValues = Record<string, string | number | boolean | null>
const configValues = ref<Record<string, ConfigValues>>({})

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

/**
 * Re-read a platform from the beginning, rather than from where the last run stopped.
 *
 * The distinction matters more than the wording suggests: an incremental sync asks only for what
 * is new, so a provider whose normalization changed leaves every older record exactly as it was
 * first stored. This is the only way to make the archive reflect a corrected mapping — and the
 * reason it is a separate button is that it re-walks an entire history, which is a great many
 * requests to someone else's server.
 */
function fullResync(id: string): Promise<void> {
  return act(id, 'Queueing a full resync', async () => {
    await syncProvider(id, 'full')
    await list.reload()
    return 'Full resync queued. It re-reads the whole history, so it may take a while.'
  })
}

function showLatestRun(id: string): Promise<void> {
  return act(id, 'Loading latest run', async () => {
    const result = await latestProviderRun(id)
    if (result.status === 'success') return 'Latest run completed successfully.'
    return `Latest run ${result.status ?? 'not yet known'} (${result.error_class ?? 'no error class'}): ${result.detail ?? 'no detail given'}`
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
    return 'Configuration saved. Enable the provider when you are ready to sync.'
  })
}
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
      <!-- The lead provider takes the full row; every other tile is half a row, so the rest always
           pair up. A row-spanning tile here would leave the next two tiles stacked beside it. -->
      <li
        v-for="(provider, index) in sorted"
        :key="provider.id"
        :class="['card', index === 0 ? 'span-4' : 'span-2']"
      >
        <div class="card__head">
          <h2>{{ provider.name }}</h2>
          <!-- : an unreviewed drop-in provider is labelled as such, always. -->
          <span v-if="!provider.reviewed" class="badge badge--warn">unreviewed</span>
          <ProviderStatus :status="provider.status" />
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

        <!-- : the required action leads, before any error text. -->
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

        <!-- : pinned settings are shown, disabled, with why the edit would not stick. -->
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

        <p v-if="!readonlyAccess" class="actions">
          <button type="button" :disabled="busy === provider.id" @click="toggle(provider.id, !provider.enabled)">
            {{ provider.enabled ? 'Disable' : 'Enable' }}
          </button>
          <button type="button" :disabled="busy === provider.id || !provider.enabled" @click="sync(provider.id)">
            Sync now
          </button>
          <button
            type="button"
            :disabled="busy === provider.id || !provider.enabled"
            :title="`Re-read ${provider.name} from the beginning, ignoring the saved cursor`"
            @click="fullResync(provider.id)"
          >
            Full resync
          </button>
          <button type="button" :disabled="busy === provider.id" @click="showConfigSchema(provider.id)">
            {{ configSchemas[provider.id] ? 'Hide configuration' : 'Configure provider' }}
          </button>
        </p>

        <p class="actions">
          <button type="button" :disabled="busy === provider.id" @click="showLatestRun(provider.id)">
            Show latest run
          </button>
        </p>

        <section v-if="configSchemas[provider.id]" class="subsection" :aria-label="`${provider.name} configuration`">
          <h3>Configuration fields</h3>
          <SchemaForm
            v-model="configValues[provider.id]"
            :schema="configSchemas[provider.id]!"
            :disabled="busy === provider.id || Boolean(provider.file_pinned_settings?.length)"
            @submit="saveConfiguration(provider.id)"
          />
          <p v-if="provider.file_pinned_settings?.length" class="muted">
            This provider is configured by the mounted file, so web edits are unavailable.
          </p>
          <template v-else>
            <p class="muted">Existing values, including credentials, are never shown. Complete required fields to replace the saved configuration.</p>
            <button type="button" :disabled="busy === provider.id" @click="saveConfiguration(provider.id)">
              Save configuration
            </button>
          </template>
        </section>
        <ErrorState
          v-if="configSchemaFailure && configSchemaFailure.id === provider.id"
          :problem="configSchemaFailure.problem"
        />

        <div v-if="provider.capabilities.includes('file_import') && !readonlyAccess" class="import-export subsection">
          <label :for="`import-${provider.id}`">Import personal export</label>
          <input
            :id="`import-${provider.id}`"
            type="file"
            accept=".csv,.rss,.xml,text/csv,application/rss+xml,application/xml,text/xml"
            :disabled="busy === provider.id || !provider.enabled"
            @change="chooseImport"
          >
          <button
            type="button"
            :disabled="busy === provider.id || !provider.enabled || !importFile"
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
