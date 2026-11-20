<script setup lang="ts">
/**
 * One log entry per row: what, when, from where. Used by the log feed, the dashboard, and the work
 * detail view, so each surface uses the same row layout.
 *
 * Logged dates use `LoggedAt` for consistent precision handling.
 */
import type { Entry, SubjectRef } from '@/api/types'
import LoggedAt from '@/components/LoggedAt.vue'

const props = withDefaults(defineProps<{ entries: Entry[]; showWork?: boolean }>(), {
  showWork: true,
})

/** "S2E4", "track 7" — integers only, in the order the contract lists them. */
function subject(ref: SubjectRef | null | undefined): string {
  if (!ref) return ''
  const bits: string[] = []
  if (ref.season !== undefined) bits.push(`season ${ref.season}`)
  if (ref.episode !== undefined) bits.push(`episode ${ref.episode}`)
  if (ref.volume !== undefined) bits.push(`volume ${ref.volume}`)
  if (ref.chapter !== undefined) bits.push(`chapter ${ref.chapter}`)
  if (ref.disc !== undefined) bits.push(`disc ${ref.disc}`)
  if (ref.track !== undefined) bits.push(`track ${ref.track}`)
  return bits.join(', ')
}
</script>

<template>
  <ul class="entries">
    <li v-for="entry in props.entries" :key="entry.id" class="entry">
      <!-- The API supplies a local cache path. The adjacent title makes the artwork decorative;
           Work.vue already shows its poster above the entry list. -->
      <img
        v-if="props.showWork && entry.work?.image"
        class="entry__art"
        :src="entry.work.image"
        alt=""
        width="40"
        height="60"
        loading="lazy"
        decoding="async"
      />
      <div class="entry__body">
        <p class="entry__title">
          <RouterLink
            v-if="props.showWork"
            :to="{ name: 'work', params: { id: entry.work_id } }"
          >
            {{ entry.work?.title ?? 'Untitled work' }}
          </RouterLink>
          <span v-else>{{ entry.kind }}</span>
          <span v-if="entry.work?.release_year" class="muted"> ({{ entry.work.release_year }})</span>
        </p>
        <p class="entry__meta">
          <LoggedAt :at="entry.logged_at" :precision="entry.logged_precision" />
          <span aria-hidden="true"> · </span>
          <span v-if="props.showWork">{{ entry.kind }}<span aria-hidden="true"> · </span></span>
          <span>{{ entry.provider_id }}</span>
          <template v-if="subject(entry.subject_ref)">
            <span aria-hidden="true"> · </span>{{ subject(entry.subject_ref) }}
          </template>
          <template v-if="entry.progress?.value !== undefined">
            <span aria-hidden="true"> · </span>{{ entry.progress.value }} {{ entry.progress.unit }}
          </template>
          <template v-if="entry.deleted_at">
            <span aria-hidden="true"> · </span><span class="muted">removed upstream</span>
          </template>
        </p>
      </div>
    </li>
  </ul>
</template>

<style scoped>
.entries {
  list-style: none;
  margin: 0;
  padding: 0;
}

/* Rows, not cards: this list lives inside a Bento card, so each row is a hairline-separated line
   rather than another rounded surface. The hover wash bleeds into the card padding so the text
   column stays aligned with the tile heading. */
.entry {
  /* Flex rather than grid: with no poster the body is the only item, so no empty column indents
     rows that have no artwork. */
  display: flex;
  gap: var(--space-3);
  align-items: center;
  margin: 0 calc(-1 * var(--space-3));
  padding: var(--space-3);
  border-radius: var(--radius);
  transition: background var(--fast) ease-out;
}

.entry + .entry {
  position: relative;
}

.entry + .entry::before {
  content: '';
  position: absolute;
  inset: 0 var(--space-3) auto;
  border-top: 1px solid var(--border);
}

.entry:hover {
  background: color-mix(in srgb, var(--ctp-surface0) 55%, transparent);
}

.entry:hover::before,
.entry:hover + .entry::before {
  border-color: transparent;
}

.entry__body {
  min-width: 0;
}

/* Sized in the markup too, so a row does not reflow when the bytes land. The placeholder the API
   returns on a cache miss is a 1x1 GIF, which object-fit stretches to the same box. */
.entry__art {
  flex: none;
  width: 40px;
  height: 60px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  background: var(--surface-raised);
}

.entry__title {
  margin: 0;
  font-weight: 600;
}

.entry__meta {
  margin: 0.15rem 0 0;
  color: var(--text-muted);
  font-size: 0.85rem;
  font-variant-numeric: tabular-nums;
}
</style>
