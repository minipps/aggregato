<script setup lang="ts">
import { ref } from 'vue'

const props = withDefaults(defineProps<{
  subject: 'work' | 'creator'
  winnerId: string
  pending?: boolean
}>(), { pending: false })
const emit = defineEmits<{ merge: [loserIds: string[]]; cancel: [] }>()
const ids = ref('')
const ID_SEPARATOR = /[\s,]+/

function submit(): void {
  const loserIds = ids.value.split(ID_SEPARATOR).filter(Boolean).filter((id) => id !== props.winnerId)
  if (loserIds.length) emit('merge', loserIds)
}
</script>

<template>
  <form class="dialog" @submit.prevent="submit">
    <h2>Merge {{ subject }}s</h2>
    <p class="muted">The selected duplicates move into this {{ subject }}. The operation can be undone.</p>
    <label>Duplicate IDs <input v-model="ids" :disabled="props.pending" required aria-describedby="merge-help" /></label>
    <small id="merge-help">Separate IDs with commas or spaces.</small>
    <p>
      <button type="submit" :disabled="props.pending">{{ props.pending ? 'Merging…' : 'Merge' }}</button>
      <button type="button" :disabled="props.pending" @click="emit('cancel')">Cancel</button>
    </p>
  </form>
</template>
