<script setup lang="ts">
/**
 * Shared error state for API failures.
 *
 * Renders an RFC 9457 problem's title and detail, with its status as supporting context.
 */
import type { Problem } from '@/api/types'

const props = defineProps<{ problem: Problem; retryable?: boolean }>()
const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="state note note--danger" role="alert">
    <p class="state__title">{{ props.problem.title }}</p>
    <p v-if="props.problem.detail" class="state__detail">{{ props.problem.detail }}</p>
    <p v-if="props.problem.status > 0" class="state__meta">HTTP {{ props.problem.status }}</p>
    <button v-if="props.retryable" type="button" @click="emit('retry')">Try again</button>
  </div>
</template>

<style scoped>
/* .note carries the tint and radius; a bordered panel here would read as a card inside a card. */
.state {
  display: grid;
  gap: var(--space-2);
  justify-items: start;
}

.state__title {
  margin: 0;
  font-weight: 700;
}

.state__detail,
.state__meta {
  margin: 0;
  /* Full-strength on the tinted background — muted text on colour fails contrast. */
  color: inherit;
}

.state__meta {
  font-family: var(--font-mono);
  font-size: 0.85em;
  opacity: 0.85;
}
</style>
