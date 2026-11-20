<script setup lang="ts">
import type { ProviderStatus } from '@/api/types'

defineProps<{ status: ProviderStatus }>()

/** Status reuses the shared badge tones so it matches every other badge on screen. */
const TONE: Partial<Record<ProviderStatus, string>> = {
  idle: 'badge--ok',
  syncing: 'badge--accent',
  degraded: 'badge--danger',
  misconfigured: 'badge--danger',
}
</script>

<template>
  <span :class="['badge', 'badge--dot', 'provider-status', `provider-status--${status}`, TONE[status]]">{{ status }}</span>
</template>
