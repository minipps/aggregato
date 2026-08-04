<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

import { decideResolution, resolutionQueue, undoMerge } from '@/api/client'
import { readonlyAccess } from '@/api/session'
import type { ResolutionItem } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import { usePaged } from '@/api/useApi'

const queue = usePaged(() => resolutionQueue())
const active = ref(0)
const notice = ref('')

function current(): ResolutionItem | undefined { return queue.items.value[active.value] }
async function decide(decision: 'linked' | 'created' | 'ignored', targetId?: string): Promise<void> {
  const item = current(); if (!item || readonlyAccess.value) return
  const log = await decideResolution(item.id, decision, targetId)
  queue.items.value.splice(active.value, 1)
  active.value = Math.min(active.value, Math.max(0, queue.items.value.length - 1))
  notice.value = `Decision saved. Undo: ${log.id}`
}
async function undo(): Promise<void> { const id = Number(notice.value.split(': ')[1]); await undoMerge(id); notice.value = 'Decision undone. Reload the queue to review it again.' }
function keys(event: KeyboardEvent): void {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return
  const item = current(); if (!item) return
  if (event.key === 'ArrowDown' || event.key === 'j') { event.preventDefault(); active.value = Math.min(active.value + 1, queue.items.value.length - 1) }
  if (event.key === 'ArrowUp' || event.key === 'k') { event.preventDefault(); active.value = Math.max(active.value - 1, 0) }
  if (event.key === 'c') void decide('created')
  if (event.key === 'i') void decide('ignored')
  if (/^[1-9]$/.test(event.key)) { const candidate = item.candidates[Number(event.key) - 1]; if (candidate) void decide('linked', candidate.id) }
}
onMounted(() => window.addEventListener('keydown', keys))
onUnmounted(() => window.removeEventListener('keydown', keys))
</script>

<template>
  <section>
    <div class="bento">
      <section class="card card--accent span-2">
        <div class="card__head">
          <span class="card__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M8 7h8m0 0-3-3m3 3-3 3M16 17H8m0 0 3-3m-3 3 3 3" />
            </svg>
          </span>
          <h1>Resolution queue</h1>
        </div>
        <p>↑/↓ or j/k selects an item<template v-if="!readonlyAccess"> · 1–9 links a candidate · c creates · i ignores</template></p>
      </section>

      <section class="card" aria-labelledby="queue-count">
        <p id="queue-count" class="metric-label">Waiting</p>
        <p class="metric">{{ queue.items.value.length }}</p>
      </section>

      <section class="card" aria-labelledby="last-decision">
        <p id="last-decision" class="metric-label">Last decision</p>
        <p role="status">{{ notice || 'None yet.' }}</p>
        <p v-if="notice.includes('Undo:')" class="actions">
          <button type="button" @click="undo">Undo</button>
        </p>
      </section>
    </div>

    <LoadingState v-if="queue.loading.value" label="Loading resolution queue…" />
    <ErrorState v-else-if="queue.error.value" :problem="queue.error.value" retryable @retry="queue.restart()" />
    <EmptyState v-else-if="!queue.items.value.length" title="Queue clear" detail="There are no identity decisions waiting for you." />
    <template v-else>
      <ol class="bento queue">
        <li
          v-for="(item, index) in queue.items.value"
          :key="item.id"
          :class="['card', 'span-2', index === active ? 'active' : '']"
          tabindex="0"
          @focus="active = index"
        >
          <h2>{{ item.subject }} · {{ item.suggestion_kind ?? 'ambiguous match' }}</h2>
          <p class="muted">Proposed: {{ item.proposed }}</p>
          <ol class="candidates">
            <li v-for="(candidate, candidateIndex) in item.candidates" :key="candidate.id">
              <button v-if="!readonlyAccess" type="button" @click="decide('linked', candidate.id)">Link {{ candidateIndex + 1 }}</button>
              {{ candidate.label ?? candidate.name ?? candidate.id }}
              <span class="muted">— {{ candidate.reason }}</span>
            </li>
          </ol>
          <p v-if="!readonlyAccess" class="actions">
            <button type="button" @click="decide('created')">Create separate {{ item.subject }}</button>
            <button type="button" @click="decide('ignored')">Ignore</button>
          </p>
        </li>
      </ol>
      <p v-if="!queue.done.value" class="actions">
        <button type="button" @click="queue.loadMore()">Load more</button>
      </p>
    </template>
  </section>
</template>

<style scoped>
/* Selection is a ring on the tile, not a heavier border: the tile keeps the same footprint whether
   it is the active one or not. */
.queue > li.active {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 35%, transparent);
}

.candidates {
  margin: 0;
  padding-left: var(--space-6);
  display: grid;
  gap: var(--space-2);
}
</style>
