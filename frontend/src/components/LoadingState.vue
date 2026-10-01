<script setup lang="ts">
/** Shared loading state for API-backed views. */
const props = withDefaults(defineProps<{ label?: string }>(), { label: 'Loading…' })
</script>

<template>
  <!-- Polite live region: a screen reader hears that work is in progress without losing its place. -->
  <p class="state" role="status" aria-live="polite">{{ props.label }}</p>
</template>

<style scoped>
.state {
  color: var(--text-muted);
  padding: var(--space-4) 0;
}
</style>
