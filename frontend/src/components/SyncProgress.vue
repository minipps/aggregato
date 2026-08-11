<script setup lang="ts">
import { computed } from 'vue'

import type { RunPhase, SyncRun } from '@/api/types'
import LoggedAt from './LoggedAt.vue'

const props = defineProps<{ run: SyncRun }>()

const PHASE_LABELS: Record<RunPhase, string> = {
  starting: 'Starting',
  checking: 'Checking credentials',
  replaying: 'Replaying retained payloads',
  fetching: 'Fetching records',
  ingesting: 'Writing to the archive',
  finalizing: 'Finishing sync',
  finished: 'Finished',
  failed: 'Stopped with an error',
}

const phaseLabel = computed(() => PHASE_LABELS[props.run.phase] ?? props.run.phase)
const percent = computed(() => props.run.progress_percent ?? null)
const summary = computed(() => {
  if (percent.value !== null) return `${percent.value}% · ${props.run.items_seen} of ${props.run.progress_total} records`
  return `${props.run.items_seen} record${props.run.items_seen === 1 ? '' : 's'} seen · total not reported by provider`
})
</script>

<template>
  <section class="sync-progress" aria-live="polite" :aria-label="`${run.provider_id} sync progress`">
    <div class="sync-progress__head">
      <strong>{{ phaseLabel }}</strong>
      <span class="badge">{{ run.status }}</span>
    </div>
    <progress v-if="percent !== null" :value="percent" max="100">{{ percent }}%</progress>
    <div v-else class="progress-indeterminate" role="progressbar" aria-valuemin="0" aria-valuetext="Progress is indeterminate" />
    <p class="muted">{{ summary }}</p>
    <dl class="pairs">
      <dt>Written</dt><dd>{{ run.items_written }}</dd>
      <dt>Failed</dt><dd>{{ run.items_failed }}</dd>
      <dt>Checkpoints</dt><dd>{{ run.checkpoint_count ?? 0 }}</dd>
      <dt>Updated</dt><dd><LoggedAt :at="run.updated_at" precision="exact" /></dd>
      <dt>Last checkpoint</dt>
      <dd>
        <LoggedAt v-if="run.last_checkpoint_at" :at="run.last_checkpoint_at" precision="exact" />
        <span v-else class="muted">—</span>
      </dd>
    </dl>
    <p v-if="run.error_message" class="note note--danger">{{ run.error_class }}: {{ run.error_message }}</p>
  </section>
</template>

<style scoped>
.sync-progress__head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.sync-progress progress { display: block; width: 100%; margin: var(--space-3) 0 var(--space-2); accent-color: var(--accent); }
.progress-indeterminate { position: relative; overflow: hidden; height: 0.55rem; margin: var(--space-3) 0 var(--space-2); border-radius: 999px; background: var(--ctp-surface1); }
.progress-indeterminate::after { content: ''; position: absolute; inset: 0 auto 0 0; width: 40%; border-radius: inherit; background: var(--accent); animation: progress-slide 1.4s ease-in-out infinite; }
.pairs { margin-top: var(--space-2); }
@keyframes progress-slide { from { transform: translateX(-100%); } to { transform: translateX(250%); } }
@media (prefers-reduced-motion: reduce) { .progress-indeterminate::after { animation: none; width: 55%; } }
</style>
