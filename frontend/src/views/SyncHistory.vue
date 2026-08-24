<script setup lang="ts">
import { computed, ref } from 'vue'

import { providerRuns, providers, syncRunDiagnostics } from '@/api/client'
import { useSyncStream } from '@/api/syncStream'
import { usePaged, useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'
import SyncProgress from '@/components/SyncProgress.vue'

const available = useRequest(providers)
const sync = useSyncStream()
const providerId = ref('')
const history = usePaged(() => providerRuns(providerId.value))
const currentRun = computed(() =>
  sync.snapshot.value?.runs.find(
    (run) => run.provider_id === providerId.value && run.status === 'running',
  ),
)
const queued = computed(() =>
  sync.snapshot.value?.providers.find((provider) => provider.id === providerId.value)?.requested_mode,
)
const visibleRuns = computed(() => {
  const live = sync.snapshot.value?.runs.find((run) => run.provider_id === providerId.value)
  if (!live) return history.items.value
  const existing = history.items.value.findIndex((run) => run.id === live.id)
  if (existing < 0) return [live, ...history.items.value]
  return history.items.value.map((run, index) => (index === existing ? live : run))
})
const grouped = computed(() => {
  const groups = new Map<string, typeof visibleRuns.value>()
  for (const run of visibleRuns.value) groups.set(run.lineage_id, [...(groups.get(run.lineage_id) ?? []), run])
  return [...groups.values()]
})
const expanded = ref<number | null>(null)
const diagnostics = ref<Record<number, Awaited<ReturnType<typeof syncRunDiagnostics>>> >({})
const diagnosticError = ref<Record<number, string>>({})
async function showDiagnostics(run: (typeof visibleRuns.value)[number]): Promise<void> {
  if (expanded.value === run.id) { expanded.value = null; return }
  expanded.value = run.id
  if (diagnostics.value[run.id]) return
  try { diagnostics.value[run.id] = await syncRunDiagnostics(providerId.value, run.id) }
  catch (error) { diagnosticError.value[run.id] = error instanceof Error ? error.message : String(error) }
}

async function select(): Promise<void> { await history.restart() }
</script>

<template>
  <section>
    <div class="bento">
      <section class="card card--accent span-2">
        <div class="card__head">
          <span class="card__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M4 9a8 8 0 0 1 13-2.5L20 9m0 6a8 8 0 0 1-13 2.5L4 15" />
              <path d="M20 4v5h-5M4 20v-5h5" />
            </svg>
          </span>
          <h1>Sync history</h1>
        </div>
        <p>Every attempt a provider has made, grouped by the run it retried.</p>
      </section>

      <form class="card span-2" @submit.prevent="select">
        <h2 class="visually-hidden">Choose a provider</h2>
        <div class="field">
          <label for="history-provider">Provider</label>
          <select id="history-provider" v-model="providerId" @change="select">
            <option value="">Choose a provider</option>
            <option v-for="provider in available.data.value ?? []" :key="provider.id" :value="provider.id">{{ provider.name }}</option>
          </select>
        </div>
        <p v-if="!providerId" class="muted">Choose a provider to inspect its sync attempts.</p>
      </form>
    </div>

    <template v-if="providerId">
      <section v-if="currentRun" class="card" aria-labelledby="current-sync-heading">
        <h2 id="current-sync-heading">Sync in progress</h2>
        <SyncProgress :run="currentRun" />
      </section>
      <p v-else-if="queued" class="card note" role="status">
        {{ queued }} sync queued; waiting for the scheduler to start.
      </p>
      <LoadingState v-if="history.loading.value" label="Loading sync history…" />
      <ErrorState v-else-if="history.error.value" :problem="history.error.value" retryable @retry="history.restart()" />
      <EmptyState v-else-if="grouped.length === 0" title="No sync attempts" detail="Attempts appear here once this provider runs." />
      <ol v-else class="bento">
        <!-- The newest lineage is usually the one being diagnosed, so it takes the full row; the
             rest pair up two to a row rather than stacking beside a row-spanning tile. -->
        <li
          v-for="(attempts, index) in grouped"
          :key="attempts[0]?.lineage_id"
          :class="['card', index === 0 ? 'span-4' : 'span-2']"
        >
          <h2>Sync lineage · {{ attempts.length }} attempt{{ attempts.length === 1 ? '' : 's' }}</h2>
          <ul class="plain">
            <li v-for="run in attempts" :key="run.id">
              <button type="button" class="link-button" :aria-expanded="expanded === run.id" @click="showDiagnostics(run)">
                <strong>{{ run.status }}</strong> · attempt {{ run.attempt }}
              </button> ·
              <LoggedAt :at="run.started_at" precision="exact" /> ·
              {{ run.items_seen }} seen · {{ run.items_written }} written, {{ run.items_failed }} failed ·
              {{ run.checkpoint_count }} checkpoints
              <span v-if="run.phase !== 'finished' && run.phase !== 'failed'"> · {{ run.phase }}</span>
              <p v-if="run.error_message" class="muted">{{ run.error_class }}: {{ run.error_message }}</p>
              <div v-if="expanded === run.id" class="diagnostics">
                <LoadingState v-if="!diagnostics[run.id] && !diagnosticError[run.id]" label="Loading diagnostics…" />
                <p v-if="diagnosticError[run.id]" class="error">{{ diagnosticError[run.id] }}</p>
                <template v-if="diagnostics[run.id]">
                  <h3>Full log</h3>
                  <pre>{{ diagnostics[run.id]!.log || 'No log output.' }}</pre>
                  <h3>Raw responses</h3>
                  <p v-if="diagnostics[run.id]!.raw_responses.length === 0" class="muted">No HTTP responses captured.</p>
                  <details v-for="(response, responseIndex) in diagnostics[run.id]!.raw_responses" :key="responseIndex">
                    <summary>{{ response.status }} {{ response.method }} {{ response.url }}</summary>
                    <pre>{{ response.body }}</pre>
                  </details>
                </template>
              </div>
            </li>
          </ul>
        </li>
      </ol>
      <p v-if="!history.done.value" class="actions">
        <button type="button" @click="history.loadMore()">Load more</button>
      </p>
    </template>
  </section>
</template>
