<script setup lang="ts">
import { ref } from 'vue'

import { archiveSettings, downloadArchive, toProblem, updateArchiveSettings } from '@/api/client'
import type { Problem } from '@/api/types'
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
  <section>
    <h1>Settings</h1>
    <LoadingState v-if="state.loading.value" label="Loading archive settings…" />
    <ErrorState v-else-if="state.error.value" :problem="state.error.value" retryable @retry="state.reload()" />
    <template v-else-if="state.data.value">
      <form @submit.prevent="save">
        <h2>Retention</h2>
        <p class="muted">Failures are kept longer than successful runs by default so diagnosis remains possible.</p>
        <label>Raw payload retention (days)<input v-model.number="state.data.value.raw_payload_retention_days" min="0" type="number"></label>
        <label>Successful run retention (days)<input v-model.number="state.data.value.success_run_retention_days" min="0" type="number"></label>
        <label>Failed and partial run retention (days)<input v-model.number="state.data.value.failure_run_retention_days" min="0" type="number"></label>
        <label><input v-model="state.data.value.image_cache_enabled" type="checkbox"> Cache platform images locally</label>
        <p v-if="!state.data.value.image_cache_enabled" role="alert">Images will use a local placeholder. Turning this off clears cached image data during cleanup.</p>
        <button :disabled="busy" type="submit">Save settings</button>
      </form>
      <section>
        <h2>Storage usage</h2>
        <dl class="pairs">
          <dt>Retained raw payloads</dt><dd>{{ bytes(state.data.value.storage.raw_payload_bytes) }}</dd>
          <dt>Image cache</dt><dd>{{ bytes(state.data.value.storage.image_cache_bytes) }}</dd>
          <dt>Database</dt><dd>{{ bytes(state.data.value.storage.database_bytes) }}</dd>
        </dl>
      </section>
      <section>
        <h2>Portable archive</h2>
        <p>Download your data, image cache, and safe configuration. Secrets and active sessions are excluded.</p>
        <button :disabled="busy" type="button" @click="exportNow">Download archive</button>
      </section>
      <ErrorState v-if="error" :problem="error" />
    </template>
  </section>
</template>
