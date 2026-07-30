<script setup lang="ts">
import { creators } from '@/api/client'
import { usePaged } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'

const page = usePaged(creators)
</script>

<template>
  <section>
    <h1>Creators</h1>
    <ErrorState v-if="page.error.value" :problem="page.error.value" retryable @retry="page.restart()" />
    <LoadingState v-else-if="page.loading.value" label="Loading creators…" />
    <ul v-else-if="page.items.value.length" class="plain">
      <li v-for="creator in page.items.value" :key="creator.id">
        <RouterLink :to="{ name: 'creator', params: { id: creator.id } }">{{ creator.name }}</RouterLink>
        <span class="muted"> · {{ creator.kind }} · {{ creator.logged_count ?? 0 }} logged</span>
      </li>
    </ul>
    <EmptyState v-else title="No creators" detail="Creators appear when providers supply credits." />
    <button v-if="!page.done.value" type="button" @click="page.loadMore()">Load more</button>
  </section>
</template>
