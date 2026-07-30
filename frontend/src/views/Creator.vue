<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute } from 'vue-router'

import { creator } from '@/api/client'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import { useRequest } from '@/api/useApi'

const route = useRoute()
const id = computed(() => String(route.params['id'] ?? ''))
const detail = useRequest(() => creator(id.value))
watch(id, () => void detail.reload())
</script>

<template>
  <section>
    <LoadingState v-if="detail.loading.value" label="Loading creator…" />
    <ErrorState v-else-if="detail.error.value" :problem="detail.error.value" retryable @retry="detail.reload()" />
    <template v-else-if="detail.data.value">
      <h1>{{ detail.data.value.name }}</h1>
      <p class="muted">{{ detail.data.value.kind }} · {{ detail.data.value.logged_count ?? 0 }} logged works</p>
      <section v-for="(credits, role) in detail.data.value.credits_by_role ?? {}" :key="role">
        <h2>{{ role }}</h2>
        <ul class="plain"><li v-for="credit in credits" :key="credit.id">{{ credit.source }} · linked by {{ credit.link_confidence }}</li></ul>
      </section>
    </template>
  </section>
</template>
