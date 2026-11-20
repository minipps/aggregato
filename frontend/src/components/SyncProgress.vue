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
      <span class="badge badge--accent badge--dot">{{ run.status }}</span>
    </div>
    <progress v-if="percent !== null" :value="percent" max="100">{{ percent }}%</progress>
    <div v-else class="progress-indeterminate" role="progressbar" aria-valuemin="0" aria-valuetext="Progress is indeterminate" />
    <p class="muted">{{ summary }}</p>
    <!-- A strip of small figures rather than label/value rows: on a wide tile, rows would push each
         value a full screen-width away from its label. -->
    <dl class="sync-progress__stats">
      <div><dt>Written</dt><dd>{{ run.items_written }}</dd></div>
      <div><dt>Failed</dt><dd :class="{ 'sync-progress__failed': run.items_failed > 0 }">{{ run.items_failed }}</dd></div>
      <div><dt>Checkpoints</dt><dd>{{ run.checkpoint_count ?? 0 }}</dd></div>
      <div><dt>Updated</dt><dd><LoggedAt :at="run.updated_at" precision="exact" /></dd></div>
      <div>
        <dt>Last checkpoint</dt>
        <dd>
          <LoggedAt v-if="run.last_checkpoint_at" :at="run.last_checkpoint_at" precision="exact" />
          <span v-else class="muted">—</span>
        </dd>
      </div>
    </dl>
    <p v-if="run.error_message" class="note note--danger">{{ run.error_class }}: {{ run.error_message }}</p>
  </section>
</template>

<style scoped>
.sync-progress { display: grid; gap: var(--space-2); }
.sync-progress p { margin: 0; }
.sync-progress__head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
.sync-progress progress,
.progress-indeterminate { display: block; width: 100%; height: 0.5rem; margin: var(--space-1) 0; }
.progress-indeterminate { position: relative; overflow: hidden; border-radius: var(--radius-pill); background: var(--ctp-surface0); }
.progress-indeterminate::after { content: ''; position: absolute; inset: 0 auto 0 0; width: 40%; border-radius: inherit; background: var(--accent); animation: progress-slide 1.4s ease-in-out infinite; }
.sync-progress__stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: var(--space-3) var(--space-4); margin: var(--space-2) 0 0; }
.sync-progress__stats dt { color: var(--text-muted); font-size: 0.75rem; font-weight: 600; }
.sync-progress__stats dd { margin: 0; font-weight: 650; font-variant-numeric: tabular-nums; }
.sync-progress__failed { color: var(--danger); }
@keyframes progress-slide { from { transform: translateX(-100%); } to { transform: translateX(250%); } }
@media (prefers-reduced-motion: reduce) { .progress-indeterminate::after { animation: none; width: 55%; } }
</style>
