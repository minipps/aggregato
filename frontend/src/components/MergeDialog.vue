<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{ subject: 'work' | 'creator'; winnerId: string }>()
const emit = defineEmits<{ merge: [loserIds: string[]]; cancel: [] }>()
const ids = ref('')

function submit(): void {
  const loserIds = ids.value.split(/[\s,]+/).filter(Boolean).filter((id) => id !== props.winnerId)
  if (loserIds.length) emit('merge', loserIds)
}
</script>

<template>
  <form class="dialog" @submit.prevent="submit">
    <h2>Merge {{ subject }}s</h2>
    <p class="muted">The selected duplicates move into this {{ subject }}. The operation can be undone.</p>
    <label>Duplicate IDs <input v-model="ids" required aria-describedby="merge-help" /></label>
    <small id="merge-help">Separate IDs with commas or spaces.</small>
    <p><button type="submit">Merge</button> <button type="button" @click="emit('cancel')">Cancel</button></p>
  </form>
</template>
