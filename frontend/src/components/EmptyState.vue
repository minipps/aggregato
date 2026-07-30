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
.state {
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  padding: var(--space-6);
  text-align: center;
}

.state__title {
  margin: 0;
  font-weight: 600;
}

.state__detail {
  margin: var(--space-2) 0 0;
  color: var(--text-muted);
}
</style>
