<script setup lang="ts">
/**
 * A deliberately small JSON Schema renderer for provider settings.
 *
 * Provider config models are flat Pydantic models today. Rendering that portable subset rather
 * than provider-specific forms keeps a new provider from requiring a UI change. Unknown schemas
 * still show their property names as text inputs instead of silently hiding configuration.
 */
import { computed, ref, watch } from 'vue'

import type { JsonSchema } from '@/api/types'

const props = withDefaults(defineProps<{
  schema: JsonSchema
  modelValue?: Record<string, string | number | boolean | null>
  disabled?: boolean
}>(), {
  modelValue: () => ({}),
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, string | number | boolean | null>]
  submit: []
}>()

const values = ref<Record<string, string | number | boolean | null>>({})
const properties = computed(() => Object.entries(props.schema.properties ?? {}))

function resetValues(): void {
  values.value = Object.fromEntries(properties.value.map(([name, field]) => [
    name,
    props.modelValue[name] ?? field.default ?? (field.type === 'boolean' ? false : ''),
  ]))
}

watch(() => [props.schema, props.modelValue] as const, resetValues, { immediate: true, deep: true })

function acceptsNull(field: JsonSchema): boolean {
  return field.type === 'null' || field.anyOf?.some((option) => option.type === 'null') === true
}

function update(name: string, field: JsonSchema, event: Event): void {
  const input = event.target as HTMLInputElement | HTMLSelectElement
  const value: string | number | boolean | null = field.type === 'boolean'
    ? (input as HTMLInputElement).checked
    : field.type === 'number' || field.type === 'integer'
      ? Number(input.value)
      : acceptsNull(field) && input.value.trim() === ''
        ? null
        : input.value
  values.value = { ...values.value, [name]: value }
  emit('update:modelValue', values.value)
}
</script>

<template>
  <form class="schema-form" @submit.prevent="emit('submit')">
    <p v-if="schema.description" class="muted">{{ schema.description }}</p>
    <div v-for="[name, field] in properties" :key="name" class="schema-form__field">
      <label :for="`config-${name}`">
        {{ field.title ?? name }}
        <span v-if="schema.required?.includes(name)" aria-label="required">*</span>
      </label>
      <select
        v-if="field.enum"
        :id="`config-${name}`"
        :value="values[name]"
        :disabled="disabled"
        @change="update(name, field, $event)"
      >
        <option v-for="option in field.enum" :key="String(option)" :value="option">{{ option }}</option>
      </select>
      <input
        v-else-if="field.type === 'boolean'"
        :id="`config-${name}`"
        type="checkbox"
        :checked="Boolean(values[name])"
        :disabled="disabled"
        @change="update(name, field, $event)"
      >
      <input
        v-else
        :id="`config-${name}`"
        :type="field.writeOnly ? 'password' : field.type === 'number' || field.type === 'integer' ? 'number' : 'text'"
        :value="values[name] ?? ''"
        :disabled="disabled"
        :required="schema.required?.includes(name)"
        @input="update(name, field, $event)"
      >
      <p v-if="field.description" class="muted">{{ field.description }}</p>
    </div>
    <p v-if="properties.length === 0" class="muted">This provider does not declare configuration fields.</p>
  </form>
</template>

<style scoped>
.schema-form { display: grid; gap: var(--space-3); }
.schema-form p { margin: 0; }
.schema-form__field { display: grid; gap: var(--space-1); }
.schema-form__field > label { font-weight: 600; }
.schema-form input:not([type='checkbox']), .schema-form select { max-width: 32rem; padding: var(--space-2); }
</style>
