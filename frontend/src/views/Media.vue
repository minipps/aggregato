<script setup lang="ts">
import { entries } from '@/api/client'
import type { MediaType } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
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
</script>

<template>
  <div class="bento">
    <section class="card card--accent span-4" aria-labelledby="media-heading">
      <h1 id="media-heading">Media</h1>
      <p>Five recent completed works from each media type.</p>
    </section>

    <section
      v-for="section in sections"
      :key="section.type"
      class="card span-4"
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
      <EmptyState
        v-else-if="!section.feed.data.value?.length"
        title="No recent works"
        detail="Completed entries appear here after a sync."
      />
      <ul v-else class="media-grid">
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
            <span v-else class="media-item__cover media-item__cover--empty" aria-hidden="true" />
            <span class="media-item__title">{{ entry.work?.title ?? 'Untitled work' }}</span>
            <span class="media-item__meta">
              <span v-if="entry.work?.release_year">{{ entry.work.release_year }} · </span>
              <LoggedAt :at="entry.logged_at" :precision="entry.logged_precision" />
            </span>
          </RouterLink>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.media-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--space-4);
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
  aspect-ratio: 12 / 17;
  object-fit: cover;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--surface-raised);
}

.media-item:hover .media-item__cover,
.media-item:focus-visible .media-item__cover {
  border-color: var(--accent);
}

.media-item__cover--empty {
  background: color-mix(in srgb, var(--surface-raised), transparent 35%);
}

.media-item__title {
  font-size: 0.9rem;
  line-height: 1.2;
}

.media-item__meta {
  color: var(--text-muted);
  font-size: 0.75rem;
}

@media (max-width: 40rem) {
  .media-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
