<script setup lang="ts">
/**
 * Shared error state: the only way a failure is shown to the operator.
 *
 * Takes an RFC 9457 problem detail and renders its title and detail — never a bare status code, and
 * never a blanked view (UI consistency guidance). The status is shown as supporting context only.
 */
import type { Problem } from '@/api/types'

const props = defineProps<{ problem: Problem; retryable?: boolean }>()
const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="state" role="alert">
    <p class="state__title">{{ props.problem.title }}</p>
    <p v-if="props.problem.detail" class="state__detail">{{ props.problem.detail }}</p>
    <p v-if="props.problem.status > 0" class="state__meta">HTTP {{ props.problem.status }}</p>
    <button v-if="props.retryable" type="button" @click="emit('retry')">Try again</button>
  </div>
</template>

<style scoped>
.state {
  border: 1px solid var(--danger);
  border-radius: var(--radius);
  padding: var(--space-4);
  background: var(--surface);
}

.state__title {
  margin: 0;
  font-weight: 600;
  color: var(--danger);
}

.state__detail {
  margin: var(--space-2) 0 0;
}

.state__meta {
  margin: var(--space-2) 0 0;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 0.85em;
}
</style>
