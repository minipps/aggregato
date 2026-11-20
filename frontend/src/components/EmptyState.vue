<script setup lang="ts">
/** Shared empty state. Nothing found is a normal outcome, not a failure — so it is not an alert. */
const props = defineProps<{ title: string; detail?: string }>()
</script>

<template>
  <div class="state">
    <p class="state__title">{{ props.title }}</p>
    <p v-if="props.detail" class="state__detail">{{ props.detail }}</p>
    <slot />
  </div>
</template>

<style scoped>
/* Flat, not a box: this renders inside Bento cards, and a bordered panel there would read as a
   card within a card. */
.state {
  display: grid;
  justify-items: center;
  gap: var(--space-1);
  padding: var(--space-8) var(--space-4);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-lg);
  text-align: center;
}

.state__title {
  margin: 0;
  font-weight: 650;
}

.state__detail {
  margin: 0;
  color: var(--text-muted);
  font-size: 0.9rem;
}
</style>
