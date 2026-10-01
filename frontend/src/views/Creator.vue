<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { creator, mergeCreator, splitCreator, toProblem, undoMerge } from '@/api/client'
import { readonlyAccess } from '@/api/session'
import type { Problem } from '@/api/types'
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
const undoId = ref<number | undefined>()
const busy = ref(false)
const mutationError = ref<Problem | undefined>()
const credits = computed(() => Object.values(detail.data.value?.credits_by_role ?? {}).flat())

async function saveIdentityChange(
  action: () => Promise<{ id: number }>,
  message: string,
  close: () => void,
): Promise<void> {
  if (busy.value || readonlyAccess.value !== false) return
  busy.value = true
  mutationError.value = undefined
  try {
    const log = await action()
    close()
    notice.value = message
    undoId.value = log.id
    await detail.reload()
  } catch (caught) {
    mutationError.value = toProblem(caught)
  } finally {
    busy.value = false
  }
}

function merge(loserIds: string[]): Promise<void> {
  return saveIdentityChange(
    () => mergeCreator(id.value, loserIds),
    'Creator records merged.',
    () => { merging.value = false },
  )
}

function split(creditIds: number[], newName?: string): Promise<void> {
  return saveIdentityChange(
    () => splitCreator(id.value, creditIds, newName),
    'Credits split into a new creator.',
    () => { splitting.value = false },
  )
}

async function undo(): Promise<void> {
  if (busy.value || undoId.value === undefined || readonlyAccess.value !== false) return
  busy.value = true
  mutationError.value = undefined
  try {
    await undoMerge(undoId.value)
    undoId.value = undefined
    notice.value = 'Operation undone.'
    await detail.reload()
  } catch (caught) {
    mutationError.value = toProblem(caught)
  } finally {
    busy.value = false
  }
}
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

        <section v-if="readonlyAccess === false" class="card span-2" aria-labelledby="identity-heading">
          <h2 id="identity-heading">Identity</h2>
          <p class="actions">
            <button type="button" :disabled="busy" @click="merging = true">Merge duplicate</button>
            <button type="button" :disabled="busy" @click="splitting = true">Split credits</button>
          </p>
          <MergeDialog
            v-if="merging && readonlyAccess === false"
            subject="creator"
            :winner-id="id"
            :pending="busy"
            @merge="merge"
            @cancel="merging = false"
          />
          <SplitDialog
            v-if="splitting && readonlyAccess === false"
            :credits="credits"
            :pending="busy"
            @split="split"
            @cancel="splitting = false"
          />
          <p v-if="busy" role="status">Saving change…</p>
          <ErrorState v-if="mutationError" :problem="mutationError" />
          <p v-if="notice" class="note" role="status">
            {{ notice }}
            <button v-if="undoId !== undefined" type="button" :disabled="busy" @click="undo">Undo</button>
          </p>
        </section>

        <section
          v-for="(roleCredits, role) in detail.data.value.credits_by_role ?? {}"
          :key="role"
          class="card span-2"
        >
          <h2>{{ role }}</h2>
          <ul class="plain">
            <li v-for="credit in roleCredits" :key="credit.id">
              {{ credit.source }} ·
              <span class="muted">linked by {{ credit.link_confidence }}</span>
            </li>
          </ul>
        </section>
      </div>
    </template>
  </section>
</template>
