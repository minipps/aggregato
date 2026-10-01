<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Credit } from '@/api/types'

const props = withDefaults(defineProps<{ credits: Credit[]; pending?: boolean }>(), { pending: false })
const emit = defineEmits<{ split: [creditIds: number[], newName?: string]; cancel: [] }>()
const selected = ref<number[]>([])
const name = ref('')
const count = computed(() => selected.value.length)

function submit(): void {
  if (count.value && !props.pending) emit('split', selected.value, name.value.trim() || undefined)
}
</script>

<template>
  <form class="dialog" @submit.prevent="submit">
    <h2>Split creator</h2>
    <p class="muted">
      Choose only the credits belonging to the new person. Identity confidence is shown for each.
    </p>
    <fieldset :disabled="props.pending">
      <legend>Credits to move</legend>
      <label v-for="credit in props.credits" :key="credit.id" class="choice">
        <input v-model="selected" type="checkbox" :value="credit.id" />
        {{ credit.creator_name ?? credit.source }} · {{ credit.role }} · linked by {{ credit.link_confidence }}
      </label>
    </fieldset>
    <label>New name (optional) <input v-model="name" :disabled="props.pending" /></label>
    <p>
      <button type="submit" :disabled="!count || props.pending">
        {{ props.pending ? 'Splitting…' : `Split ${count} credit${count === 1 ? '' : 's'}` }}
      </button>
      <button type="button" :disabled="props.pending" @click="emit('cancel')">Cancel</button>
    </p>
  </form>
</template>
