<script setup lang="ts">
/**
 * One log entry per row: what, when, from where. Used by the log feed, the dashboard, and the work
 * detail view, so ordering and wording stay identical across all three (Constitution III).
 *
 * Dates always go through LoggedAt — never formatted here.
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
      <!--
        Artwork comes from the local cache only (FR-033); the API hands us its own path, never the
        platform URL. alt is empty on purpose: the title sits right beside it, and a screen reader
        announcing it twice is worse than not announcing the poster at all. Only when the work is
        named here — Work.vue shows its own poster above and would otherwise repeat it per row.
      -->
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

.entry {
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--border);
  /* Flex rather than grid: with no poster the body is the only item, so no empty column indents
     rows that have no artwork. */
  display: flex;
  gap: var(--space-3);
  align-items: start;
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
  border-radius: calc(var(--radius) / 2);
  background: var(--surface);
}

.entry__title {
  margin: 0;
  font-weight: 600;
}

.entry__meta {
  margin: var(--space-1) 0 0;
  color: var(--text-muted);
  font-size: 0.9em;
}
</style>
