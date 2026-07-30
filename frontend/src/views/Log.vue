<script setup lang="ts">
/**
 * The log feed: `GET /entries` with filters and cursor paging.
 *
 * Paging is "load more", not page numbers: the contract's cursor is opaque and there is no offset
 * parameter to number pages with (FR-030). A filter change starts a new walk, because a cursor from
 * the previous query means nothing under the new one.
 */

import { reactive, ref } from 'vue'

import { entries, providers } from '@/api/client'
import type { EntryKind, EntryQuery, MediaFamily, MediaType } from '@/api/types'
import { usePaged, useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import EntryList from '@/components/EntryList.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'

const MEDIA_TYPES: MediaType[] = [
  'film',
  'tv_series',
  'tv_season',
  'book',
  'comic',
  'manga',
  'anime_series',
  'anime_season',
  'album',
  'track',
  'game',
  'podcast',
  'podcast_episode',
  'other',
]
const MEDIA_FAMILIES: MediaFamily[] = ['screen', 'print', 'audio', 'interactive', 'other']
const KINDS: EntryKind[] = ['watch', 'rewatch', 'listen', 'read', 'finish', 'progress', 'drop']

interface Filters {
  media_family: MediaFamily | ''
  media_type: MediaType | ''
  provider: string
  kind: EntryKind | ''
  from: string
  to: string
  q: string
}

const filters = reactive<Filters>({
  media_family: '',
  media_type: '',
  provider: '',
  kind: '',
  from: '',
  to: '',
  q: '',
})

/** The filter set the current cursor walk belongs to. */
const applied = ref<EntryQuery>({})

/** `<input type="date">` yields a plain date; the contract wants a date-time, so span the whole day. */
function query(): EntryQuery {
  return {
    ...(filters.media_family === '' ? {} : { media_family: filters.media_family }),
    ...(filters.media_type === '' ? {} : { media_type: filters.media_type }),
    ...(filters.provider === '' ? {} : { provider: filters.provider }),
    ...(filters.kind === '' ? {} : { kind: filters.kind }),
    ...(filters.from === '' ? {} : { from: `${filters.from}T00:00:00Z` }),
    ...(filters.to === '' ? {} : { to: `${filters.to}T23:59:59Z` }),
    ...(filters.q === '' ? {} : { q: filters.q }),
  }
}

const feed = usePaged(() => entries(applied.value))

// The provider filter offers what is actually installed. A failure here degrades the filter to a
// free-text field rather than breaking the feed.
const installed = useRequest(providers)

async function apply(): Promise<void> {
  applied.value = query()
  await feed.restart()
}
</script>

<template>
  <section>
    <h1>Log</h1>

    <form class="filters" @submit.prevent="apply">
      <h2 class="visually-hidden">Filters</h2>

      <div class="field">
        <label for="filter-q">Search</label>
        <input id="filter-q" v-model="filters.q" type="search" placeholder="Titles and reviews" />
      </div>

      <div class="field">
        <label for="filter-family">Media family</label>
        <select id="filter-family" v-model="filters.media_family">
          <option value="">Any</option>
          <option v-for="family in MEDIA_FAMILIES" :key="family" :value="family">
            {{ family }}
          </option>
        </select>
      </div>

      <div class="field">
        <label for="filter-type">Media type</label>
        <select id="filter-type" v-model="filters.media_type">
          <option value="">Any</option>
          <option v-for="type in MEDIA_TYPES" :key="type" :value="type">{{ type }}</option>
        </select>
      </div>

      <div class="field">
        <label for="filter-provider">Provider</label>
        <input
          id="filter-provider"
          v-model="filters.provider"
          list="provider-options"
          type="text"
          placeholder="Any"
        />
        <datalist id="provider-options">
          <option v-for="provider in installed.data.value ?? []" :key="provider.id" :value="provider.id">
            {{ provider.name }}
          </option>
        </datalist>
      </div>

      <div class="field">
        <label for="filter-kind">Kind</label>
        <select id="filter-kind" v-model="filters.kind">
          <option value="">Any</option>
          <option v-for="kind in KINDS" :key="kind" :value="kind">{{ kind }}</option>
        </select>
      </div>

      <div class="field">
        <label for="filter-from">Logged from</label>
        <input id="filter-from" v-model="filters.from" type="date" />
      </div>

      <div class="field">
        <label for="filter-to">Logged to</label>
        <input id="filter-to" v-model="filters.to" type="date" />
      </div>

      <button type="submit">Apply filters</button>
    </form>

    <ErrorState
      v-if="feed.error.value"
      :problem="feed.error.value"
      retryable
      @retry="feed.restart()"
    />

    <EntryList v-if="feed.items.value.length > 0" :entries="feed.items.value" />

    <LoadingState v-if="feed.loading.value" label="Loading entries…" />

    <EmptyState
      v-else-if="feed.items.value.length === 0 && !feed.error.value"
      title="No entries match"
      detail="Either nothing has synced yet, or the filters exclude everything."
    />

    <p v-if="feed.items.value.length > 0" class="paging">
      <button v-if="!feed.done.value" type="button" :disabled="feed.loading.value" @click="feed.loadMore()">
        Load more
      </button>
      <span v-else class="muted">End of the log.</span>
    </p>
  </section>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: end;
  margin-bottom: var(--space-6);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.paging {
  margin-top: var(--space-4);
}
</style>
