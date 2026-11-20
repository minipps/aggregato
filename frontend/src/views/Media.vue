<script setup lang="ts">
import { computed } from 'vue'

import { entries } from '@/api/client'
import type { MediaType } from '@/api/types'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'
import { useRequest } from '@/api/useApi'

const types: { type: MediaType; label: string }[] = [
  { type: 'anime', label: 'Anime' },
  { type: 'manga', label: 'Manga' },
  { type: 'film', label: 'Films' },
  { type: 'tv', label: 'TV' },
  { type: 'book', label: 'Books' },
  { type: 'comic', label: 'Comics' },
  { type: 'album', label: 'Albums' },
  { type: 'track', label: 'Tracks' },
  { type: 'game', label: 'Games' },
  { type: 'other', label: 'Other' },
]

const sections = types.map(({ type, label }) => ({
  type,
  label,
  feed: useRequest(async () => (await entries({ media_type: type, status: 'completed', limit: 5 }).next()).items),
}))

/** Loaded with nothing completed: listed in the shared summary tile instead of a tile of its own. */
function isEmpty(section: (typeof sections)[number]): boolean {
  return !section.feed.loading.value && !section.feed.error.value && !section.feed.data.value?.length
}

const shown = computed(() => sections.filter((section) => !isEmpty(section)))
const empty = computed(() => sections.filter(isEmpty))
</script>

<template>
  <div class="bento">
    <section class="card card--accent span-4" aria-labelledby="media-heading">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <rect x="3" y="4" width="7" height="16" rx="1.5" />
            <rect x="14" y="4" width="7" height="10" rx="1.5" />
          </svg>
        </span>
        <h1 id="media-heading">Media</h1>
      </div>
      <p>Five recent completed works from each media type.</p>
    </section>

    <section
      v-for="section in shown"
      :key="section.type"
      class="card span-2"
      :aria-labelledby="`${section.type}-heading`"
    >
      <h2 :id="`${section.type}-heading`">{{ section.label }}</h2>
      <ErrorState
        v-if="section.feed.error.value"
        :problem="section.feed.error.value"
        retryable
        @retry="section.feed.reload()"
      />
      <LoadingState v-else-if="section.feed.loading.value" />
      <ul v-else-if="section.feed.data.value" class="media-grid">
        <li v-for="entry in section.feed.data.value" :key="entry.id">
          <RouterLink class="media-item" :to="{ name: 'work', params: { id: entry.work_id } }">
            <img
              v-if="entry.work?.image"
              class="media-item__cover"
              :src="entry.work.image"
              alt=""
              width="240"
              height="340"
              loading="lazy"
              decoding="async"
            />
            <span v-else class="media-item__cover media-item__cover--empty" aria-hidden="true">{{
              (entry.work?.title ?? '?').charAt(0)
            }}</span>
            <span class="media-item__title">{{ entry.work?.title ?? 'Untitled work' }}</span>
            <span class="media-item__meta">
              <span v-if="entry.work?.release_year">{{ entry.work.release_year }} · </span>
              <LoggedAt :at="entry.logged_at" :precision="entry.logged_precision" />
            </span>
          </RouterLink>
        </li>
      </ul>
    </section>

    <!-- Types with nothing completed share one tile instead of each holding an empty row. It fills
         the half row left by an odd number of populated types. -->
    <section
      v-if="empty.length"
      :class="['card', shown.length % 2 ? 'span-2' : 'span-4']"
      aria-labelledby="media-empty-heading"
    >
      <h2 id="media-empty-heading">Nothing completed yet</h2>
      <ul class="media-empty">
        <li v-for="section in empty" :key="section.type" class="badge">{{ section.label }}</li>
      </ul>
      <p class="muted">Completed entries of these types appear here after a sync.</p>
    </section>
  </div>
</template>

<style scoped>
.media-empty {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.media-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--space-3);
  list-style: none;
  margin: 0;
  padding: 0;
}

.media-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  color: inherit;
  text-decoration: none;
}

.media-item__cover {
  display: block;
  width: 100%;
  aspect-ratio: 2 / 3;
  object-fit: cover;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-raised);
  transition: border-color var(--fast) ease-out;
}

.media-item:hover .media-item__cover,
.media-item:focus-visible .media-item__cover {
  border-color: var(--accent);
}

/* No artwork: the title's initial on a quiet surface, so a row of missing covers still differs. */
.media-item__cover--empty {
  display: grid;
  place-items: center;
  background: var(--ctp-mantle);
  color: var(--ctp-surface2);
  font-size: 2rem;
  font-weight: 700;
  text-transform: uppercase;
}

.media-item__title {
  font-size: 0.9rem;
  font-weight: 600;
  line-height: 1.25;
}

.media-item:hover .media-item__title {
  color: var(--accent);
}

@media (max-width: 40rem) {
  .media-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

.media-item__meta {
  color: var(--text-muted);
  font-size: 0.75rem;
  font-variant-numeric: tabular-nums;
}
</style>
