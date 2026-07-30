<script setup lang="ts">
import { computed, ref } from 'vue'

import { providerRuns, providers } from '@/api/client'
import { usePaged, useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'

const available = useRequest(providers)
const providerId = ref('')
const history = usePaged(() => providerRuns(providerId.value))
const grouped = computed(() => {
  const groups = new Map<string, typeof history.items.value>()
  for (const run of history.items.value) groups.set(run.lineage_id, [...(groups.get(run.lineage_id) ?? []), run])
  return [...groups.values()]
})

async function select(): Promise<void> { await history.restart() }
</script>

<template>
  <section>
    <h1>Sync history</h1>
    <label>Provider <select v-model="providerId" @change="select"><option value="">Choose a provider</option><option v-for="provider in available.data.value ?? []" :key="provider.id" :value="provider.id">{{ provider.name }}</option></select></label>
    <p v-if="!providerId" class="muted">Choose a provider to inspect its sync attempts.</p>
    <LoadingState v-else-if="history.loading.value" label="Loading sync history…" />
    <ErrorState v-else-if="history.error.value" :problem="history.error.value" retryable @retry="history.restart()" />
    <EmptyState v-else-if="grouped.length === 0" title="No sync attempts" detail="Attempts appear here once this provider runs." />
    <ol v-else class="plain">
      <li v-for="attempts in grouped" :key="attempts[0]?.lineage_id" class="card">
        <h2>Sync lineage · {{ attempts.length }} attempt{{ attempts.length === 1 ? '' : 's' }}</h2>
        <ul class="plain"><li v-for="run in attempts" :key="run.id"><strong>{{ run.status }}</strong> · attempt {{ run.attempt }} · <LoggedAt :at="run.started_at" precision="exact" /> · {{ run.items_written }} written, {{ run.items_failed }} failed <p v-if="run.error_message" class="muted">{{ run.error_class }}: {{ run.error_message }}</p></li></ul>
      </li>
    </ol>
    <button v-if="providerId && !history.done.value" type="button" @click="history.loadMore()">Load more</button>
  </section>
</template>
