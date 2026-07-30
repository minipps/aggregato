<script setup lang="ts">
/**
 * Providers: what is installed, how it acquires data, whether it is healthy, and what the operator
 * must do about it.
 *
 * Three things here are requirements rather than presentation choices:
 *   * `reviewed: false` is labelled **unreviewed** (FR-041) — a drop-in development provider must
 *     never look like a shipped one.
 *   * `last_error.action_required` is the loudest thing on a failing card (SC-005): the point of the
 *     screen is the next action, not the stack of error text.
 *   * settings listed in `file_pinned_settings` are shown as uneditable, with the reason (R15) —
 *     otherwise the UI silently discards edits the config file overrides.
 */

import { ref } from 'vue'

import {
  checkProvider,
  importProviderFile,
  providerConfigSchema,
  providers,
  setProviderEnabled,
  syncProvider,
  toProblem,
} from '@/api/client'
import type { JsonSchema, Problem } from '@/api/types'
import { useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'
import ProviderStatus from '@/components/ProviderStatus.vue'
import SchemaForm from '@/components/SchemaForm.vue'

const list = useRequest(providers)

/**
 * Action feedback, attached to the provider it belongs to: one provider's failed action reports on
 * that card only and never blanks the list (Constitution III). Buttons are disabled while an action
 * runs, so at most one is ever in flight.
 */
const busy = ref('')
const notice = ref<{ id: string; text: string } | undefined>(undefined)
const failure = ref<{ id: string; problem: Problem } | undefined>(undefined)
const importFile = ref<File | undefined>(undefined)
const configSchemas = ref<Record<string, JsonSchema | undefined>>({})
const configSchemaFailure = ref<{ id: string; problem: Problem } | undefined>(undefined)

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

function check(id: string): Promise<void> {
  return act(id, 'Checking credentials', async () => {
    const result = await checkProvider(id)
    if (result.ok) return 'Credentials accepted.'
    return `Credentials rejected (${result.error_class ?? 'unknown'}): ${result.detail ?? 'no detail given'}`
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
  } catch (caught) {
    configSchemaFailure.value = { id, problem: toProblem(caught) }
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <section>
    <h1>Providers</h1>

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

    <ul v-else class="cards">
      <li v-for="provider in list.data.value ?? []" :key="provider.id" class="card">
        <h2>
          {{ provider.name }}
          <!-- FR-041: an unreviewed drop-in provider is labelled as such, always. -->
          <span v-if="!provider.reviewed" class="badge badge--warn">unreviewed</span>
          <ProviderStatus :status="provider.status" />
        </h2>

        <p class="muted">
          {{ provider.acquisition }} · {{ provider.enabled ? 'enabled' : 'disabled' }}
          <template v-if="provider.consecutive_failures">
            · {{ provider.consecutive_failures }} consecutive failures
          </template>
        </p>

        <div v-if="!provider.reviewed" class="provider-warning" role="alert">
          This is a local drop-in provider. Review its code and configuration before enabling it.
        </div>
        <div v-if="provider.acquisition === 'scrape'" class="provider-warning" role="note">
          Scraping can trigger rate limits or blocks. Use only an account you control; Aggregato does not bypass CAPTCHA.
        </div>

        <!-- SC-005: the required action leads, before any error text. -->
        <div v-if="provider.last_error?.action_required" class="action-required" role="alert">
          <p class="action-required__title">Action required</p>
          <p>{{ provider.last_error.action_required }}</p>
          <p v-if="provider.last_error.message" class="muted">
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

        <!-- R15: pinned settings are shown, disabled, with why the edit would not stick. -->
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

        <p class="actions">
          <button type="button" :disabled="busy === provider.id" @click="toggle(provider.id, !provider.enabled)">
            {{ provider.enabled ? 'Disable' : 'Enable' }}
          </button>
          <button type="button" :disabled="busy === provider.id || !provider.enabled" @click="sync(provider.id)">
            Sync now
          </button>
          <button type="button" :disabled="busy === provider.id" @click="check(provider.id)">
            Check credentials
          </button>
          <button type="button" :disabled="busy === provider.id" @click="showConfigSchema(provider.id)">
            {{ configSchemas[provider.id] ? 'Hide configuration' : 'View configuration' }}
          </button>
        </p>

        <section v-if="configSchemas[provider.id]" class="configuration" :aria-label="`${provider.name} configuration`">
          <h3>Configuration fields</h3>
          <SchemaForm :schema="configSchemas[provider.id]!" disabled />
          <p class="muted">Configuration is managed in the local config file; these fields document what this provider accepts.</p>
        </section>
        <ErrorState
          v-if="configSchemaFailure && configSchemaFailure.id === provider.id"
          :problem="configSchemaFailure.problem"
        />

        <div v-if="provider.capabilities.includes('file_import')" class="import-export">
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
.cards {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: var(--space-4);
}

.action-required {
  border-left: 4px solid var(--danger);
  background: var(--surface);
  padding: var(--space-3);
  margin: var(--space-3) 0;
}

.action-required__title {
  margin: 0 0 var(--space-1);
  font-weight: 700;
  color: var(--danger);
}

.action-required p {
  margin: 0;
}

.pairs {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: var(--space-1) var(--space-4);
  margin: var(--space-3) 0;
}

.pairs dt {
  font-weight: 600;
  color: var(--text-muted);
}

.pairs dd {
  margin: 0;
}

.pinned {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--space-3);
}

.pinned p {
  margin: 0 0 var(--space-2);
  max-width: var(--measure);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.notice {
  margin: 0;
  color: var(--text-muted);
  min-height: 1.5em;
}

.import-export {
  display: grid;
  gap: var(--space-2);
  margin-top: var(--space-3);
}

.import-export p {
  margin: 0;
}

.provider-warning, .configuration {
  border-left: 4px solid var(--warn);
  background: var(--surface);
  padding: var(--space-3);
  margin: var(--space-3) 0;
}

.configuration h3 { margin-bottom: var(--space-2); }
</style>
