<script setup lang="ts">
import { ref } from 'vue'

import { archiveSettings, downloadArchive, toProblem, updateArchiveSettings } from '@/api/client'
import type { Problem } from '@/api/types'
import { readonlyAccess } from '@/api/session'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import { useRequest } from '@/api/useApi'

const state = useRequest(archiveSettings)
const busy = ref(false)
const error = ref<Problem | undefined>()

function bytes(value: number): string {
  return new Intl.NumberFormat(undefined, { style: 'unit', unit: 'byte', notation: 'compact' }).format(value)
}

async function save(): Promise<void> {
  if (!state.data.value) return
  busy.value = true
  error.value = undefined
  try {
    state.data.value = await updateArchiveSettings({
      raw_payload_retention_days: state.data.value.raw_payload_retention_days,
      success_run_retention_days: state.data.value.success_run_retention_days,
      failure_run_retention_days: state.data.value.failure_run_retention_days,
      image_cache_enabled: state.data.value.image_cache_enabled,
    })
  } catch (caught) { error.value = toProblem(caught) } finally { busy.value = false }
}

async function exportNow(): Promise<void> {
  busy.value = true
  error.value = undefined
  try { await downloadArchive() } catch (caught) { error.value = toProblem(caught) } finally { busy.value = false }
}
</script>

<template>
  <div class="bento">
    <section class="card card--accent span-2">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1M7.7 16.3l-2.1 2.1" />
          </svg>
        </span>
        <h1>Settings</h1>
      </div>
      <p>What this archive keeps, how much room it takes, and how to take it with you.</p>
    </section>

    <LoadingState v-if="state.loading.value" label="Loading archive settings…" />
    <ErrorState v-else-if="state.error.value" class="span-2" :problem="state.error.value" retryable @retry="state.reload()" />
    <template v-else-if="state.data.value">
      <section class="card span-2" aria-labelledby="storage-heading">
        <h2 id="storage-heading">Storage usage</h2>
        <dl class="pairs">
          <dt>Retained raw payloads</dt><dd>{{ bytes(state.data.value.storage.raw_payload_bytes) }}</dd>
          <dt>Image cache</dt><dd>{{ bytes(state.data.value.storage.image_cache_bytes) }}</dd>
          <dt>Database</dt><dd>{{ bytes(state.data.value.storage.database_bytes) }}</dd>
        </dl>
      </section>

      <form class="card card--feature span-2" @submit.prevent="save">
        <h2>Retention</h2>
        <!-- A native disabled fieldset covers every control inside it, now and when one is added. -->
        <fieldset :disabled="readonlyAccess">
          <legend class="visually-hidden">Retention windows</legend>
          <p class="muted">Failures are kept longer than successful runs by default so diagnosis remains possible.</p>
          <label>Raw payload retention (days)<input v-model.number="state.data.value.raw_payload_retention_days" min="0" type="number"></label>
          <label>Successful run retention (days)<input v-model.number="state.data.value.success_run_retention_days" min="0" type="number"></label>
          <label>Failed and partial run retention (days)<input v-model.number="state.data.value.failure_run_retention_days" min="0" type="number"></label>
          <label><input v-model="state.data.value.image_cache_enabled" type="checkbox"> Cache platform images locally</label>
          <p v-if="!state.data.value.image_cache_enabled" class="note note--warn" role="alert">Images will use a local placeholder. Turning this off clears cached image data during cleanup.</p>
        </fieldset>
        <p v-if="!readonlyAccess" class="actions">
          <button :disabled="busy" type="submit">Save settings</button>
        </p>
      </form>

      <section class="card span-2" aria-labelledby="archive-heading">
        <div class="card__head">
          <span class="card__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M12 4v10m0 0-4-4m4 4 4-4M5 19h14" />
            </svg>
          </span>
          <h2 id="archive-heading">Portable archive</h2>
        </div>
        <p>Download your data, image cache, and safe configuration. Secrets and active sessions are excluded.</p>
        <p class="actions">
          <button :disabled="busy" type="button" @click="exportNow">Download archive</button>
        </p>
      </section>

      <ErrorState v-if="error" class="span-2" :problem="error" />
    </template>
  </div>
</template>

<style scoped>
/* The fieldset exists to carry `disabled`, not to draw a box around the fields — the card already
   is the box. */
fieldset {
  display: grid;
  gap: var(--space-3);
  justify-items: start;
  border: 0;
  margin: 0;
  padding: 0;
}

fieldset label {
  gap: var(--space-3);
}
</style>
