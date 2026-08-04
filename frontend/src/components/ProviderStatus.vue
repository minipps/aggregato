<script setup lang="ts">
import type { ProviderStatus } from '@/api/types'

defineProps<{ status: ProviderStatus }>()
</script>

<template>
  <span :class="['provider-status', `provider-status--${status}`]">{{ status }}</span>
</template>

<style scoped>
.provider-status { border-radius: var(--radius-pill); padding: .1rem .55rem; font-size: .78em; font-weight: 650; background: var(--surface-raised); color: var(--text); }
.provider-status--degraded, .provider-status--misconfigured { background: color-mix(in srgb, var(--danger) 20%, var(--ctp-base)); color: var(--danger); }
.provider-status--idle, .provider-status--ok { background: color-mix(in srgb, var(--ok) 20%, var(--ctp-base)); color: var(--ok); }
.provider-status--syncing { background: color-mix(in srgb, var(--accent) 20%, var(--ctp-base)); color: var(--accent); }
</style>
