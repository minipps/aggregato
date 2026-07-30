<script setup lang="ts">
import { ref } from 'vue'

import { ingestFailures, replayFailure } from '@/api/client'
import { usePaged } from '@/api/useApi'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'

const failures = usePaged(ingestFailures)
const busy = ref<number | null>(null)
async function replay(id: number): Promise<void> { busy.value = id; try { await replayFailure(id); await failures.restart() } finally { busy.value = null } }
</script>

<template>
  <section><h1>Ingest failures</h1>
    <LoadingState v-if="failures.loading.value" label="Loading retained records…" />
    <ErrorState v-else-if="failures.error.value" :problem="failures.error.value" retryable @retry="failures.restart()" />
    <p v-else-if="failures.items.value.length === 0" class="muted">No unresolved ingest failures.</p>
    <ul v-else class="plain"><li v-for="failure in failures.items.value" :key="failure.id" class="card"><h2>{{ failure.provider_id }} · {{ failure.stage }}</h2><p>{{ failure.error }}</p><details><summary>Stored payload</summary><pre>{{ JSON.stringify(failure.raw_payload, null, 2) }}</pre></details><button type="button" :disabled="busy === failure.id" @click="replay(failure.id)">Replay after provider fix</button></li></ul>
    <button v-if="!failures.done.value" type="button" @click="failures.loadMore()">Load more</button>
  </section>
</template>
