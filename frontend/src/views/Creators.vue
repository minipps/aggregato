<script setup lang="ts">
import { creators } from '@/api/client'
import { usePaged } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'

const page = usePaged(creators)
</script>

<template>
  <div class="bento">
    <section class="card card--accent span-3">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <circle cx="9" cy="8" r="3.5" />
            <path d="M2.5 20a6.5 6.5 0 0 1 13 0M16 5a3.5 3.5 0 0 1 0 7m1 8a6.5 6.5 0 0 0-2-4.7" />
          </svg>
        </span>
        <h1>Creators</h1>
      </div>
      <p>Everyone credited on something in the archive, as the providers reported them.</p>
    </section>

    <section class="card" aria-labelledby="creators-loaded">
      <p id="creators-loaded" class="metric-label">Loaded</p>
      <p class="metric">{{ page.items.value.length }}</p>
    </section>

    <section class="card span-4" aria-labelledby="creator-list-heading">
      <h2 id="creator-list-heading" class="visually-hidden">Creator list</h2>
      <ErrorState v-if="page.error.value" :problem="page.error.value" retryable @retry="page.loadMore()" />
      <LoadingState v-else-if="page.loading.value" label="Loading creators…" />
      <ul v-else-if="page.items.value.length" class="plain">
        <li v-for="creator in page.items.value" :key="creator.id">
          <RouterLink :to="{ name: 'creator', params: { id: creator.id } }">{{ creator.name }}</RouterLink>
          <span class="muted"> · {{ creator.kind }} · {{ creator.logged_count ?? 0 }} logged</span>
        </li>
      </ul>
      <EmptyState v-else title="No creators" detail="Creators appear when providers supply credits." />
      <p v-if="!page.done.value" class="actions">
        <button type="button" @click="page.loadMore()">Load more</button>
      </p>
    </section>
  </div>
</template>
