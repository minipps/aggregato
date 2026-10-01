<script setup lang="ts">
import { ref, watch } from 'vue'

import { ingestFailures, replayFailure } from '@/api/client'
import { readonlyAccess } from '@/api/session'
import type { IngestFailure } from '@/api/types'
import { usePaged } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'

const failures = usePaged<IngestFailure>(() => readonlyAccess.value === false
  ? ingestFailures()
  : { next: async () => ({ items: [], done: true }) })
const busy = ref<number | null>(null)
watch(readonlyAccess, (canReadPayloads) => {
  if (canReadPayloads === false) void failures.restart()
})
async function replay(id: number): Promise<void> {
  if (readonlyAccess.value !== false || busy.value !== null) return
  busy.value = id
  try {
    await replayFailure(id)
    await failures.restart()
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <section>
    <div class="bento">
      <section class="card card--accent span-3">
        <div class="card__head">
          <span class="card__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M12 8v5m0 3h.01M10.3 3.9 2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
            </svg>
          </span>
          <h1>Ingest failures</h1>
        </div>
        <p>Records a provider handed over that could not be stored. The payload is kept so it can be replayed.</p>
      </section>

      <section class="card" aria-labelledby="failures-count">
        <p id="failures-count" class="metric-label">Retained</p>
        <p class="metric">{{ readonlyAccess === false ? failures.items.value.length : '—' }}</p>
      </section>
    </div>

    <EmptyState
      v-if="readonlyAccess !== false"
      title="Operator access required"
      detail="Ingest failure payloads are available only to an operator."
    />
    <LoadingState v-else-if="failures.loading.value" label="Loading retained records…" />
    <ErrorState v-else-if="failures.error.value" :problem="failures.error.value" retryable @retry="failures.loadMore()" />
    <EmptyState v-else-if="failures.items.value.length === 0" title="Nothing to replay" detail="No unresolved ingest failures." />
    <template v-else>
      <ul class="bento">
        <!-- Newest failure across the full row; the rest pair up two to a row. -->
        <li
          v-for="(failure, index) in failures.items.value"
          :key="failure.id"
          :class="['card', index === 0 ? 'span-4' : 'span-2']"
        >
          <h2>{{ failure.provider_id }} · {{ failure.stage }}</h2>
          <p class="note note--danger">{{ failure.error }}</p>
          <details>
            <summary>Stored payload</summary>
            <pre>{{ JSON.stringify(failure.raw_payload, null, 2) }}</pre>
          </details>
          <p v-if="readonlyAccess === false" class="actions">
            <button type="button" :disabled="busy === failure.id" @click="replay(failure.id)">Replay after provider fix</button>
          </p>
        </li>
      </ul>
      <p v-if="!failures.done.value" class="actions">
        <button type="button" @click="failures.loadMore()">Load more</button>
      </p>
    </template>
  </section>
</template>
