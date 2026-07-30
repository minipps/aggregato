<script setup lang="ts">
import type { ProviderStatus } from '@/api/types'

defineProps<{ status: ProviderStatus }>()
</script>

<template>
  <span :class="['provider-status', `provider-status--${status}`]">{{ status }}</span>
</template>

<style scoped>
.provider-status { border-radius: 999px; padding: .15rem .5rem; font-size: .85em; font-weight: 700; background: var(--surface); }
.provider-status--degraded, .provider-status--misconfigured { color: var(--danger); border: 1px solid var(--danger); }
.provider-status--idle { color: var(--ok); }
</style>
