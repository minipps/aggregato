<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { mergeCreator, splitCreator, undoMerge, creator } from '@/api/client'
import { readonlyAccess } from '@/api/session'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import MergeDialog from '@/components/MergeDialog.vue'
import SplitDialog from '@/components/SplitDialog.vue'
import { useRequest } from '@/api/useApi'

const route = useRoute()
const id = computed(() => String(route.params['id'] ?? ''))
const detail = useRequest(() => creator(id.value))
watch(id, () => void detail.reload())
const merging = ref(false)
const splitting = ref(false)
const notice = ref('')
const credits = computed(() => Object.values(detail.data.value?.credits_by_role ?? {}).flat())
async function merge(loserIds: string[]): Promise<void> { const log = await mergeCreator(id.value, loserIds); merging.value = false; notice.value = `Merged. Undo: ${log.id}`; await detail.reload() }
async function split(creditIds: number[], newName?: string): Promise<void> { const log = await splitCreator(id.value, creditIds, newName); splitting.value = false; notice.value = `Split. Undo: ${log.id}`; await detail.reload() }
async function undo(id: number): Promise<void> { await undoMerge(id); notice.value = 'Operation undone.'; await detail.reload() }
</script>

<template>
  <section>
    <LoadingState v-if="detail.loading.value" label="Loading creator…" />
    <ErrorState v-else-if="detail.error.value" :problem="detail.error.value" retryable @retry="detail.reload()" />
    <template v-else-if="detail.data.value">
      <div class="bento">
        <section class="card card--accent span-2">
          <div class="card__head">
            <span class="card__icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <circle cx="12" cy="8" r="4" />
                <path d="M5 21a7 7 0 0 1 14 0" />
              </svg>
            </span>
            <h1>{{ detail.data.value.name }}</h1>
          </div>
          <p>{{ detail.data.value.kind }} · {{ detail.data.value.logged_count ?? 0 }} logged works</p>
        </section>

        <section v-if="!readonlyAccess" class="card span-2" aria-labelledby="identity-heading">
          <h2 id="identity-heading">Identity</h2>
          <p class="actions">
            <button type="button" @click="merging = true">Merge duplicate</button>
            <button type="button" @click="splitting = true">Split credits</button>
          </p>
          <MergeDialog v-if="merging" subject="creator" :winner-id="id" @merge="merge" @cancel="merging = false" />
          <SplitDialog v-if="splitting" :credits="credits" @split="split" @cancel="splitting = false" />
          <p v-if="notice" class="note" role="status">
            {{ notice }}
            <button v-if="notice.includes('Undo:')" type="button" @click="undo(Number(notice.split(': ')[1]))">Undo</button>
          </p>
        </section>

        <section v-for="(roleCredits, role) in detail.data.value.credits_by_role ?? {}" :key="role" class="card span-2">
          <h2>{{ role }}</h2>
          <ul class="plain"><li v-for="credit in roleCredits" :key="credit.id">{{ credit.source }} · <span class="muted">linked by {{ credit.link_confidence }}</span></li></ul>
        </section>
      </div>
    </template>
  </section>
</template>
