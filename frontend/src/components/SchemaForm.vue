<script setup lang="ts">
/** Render the flat scalar and nullable JSON Schema subset used by provider settings. */
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
  values.value = Object.fromEntries(properties.value.map(([name, field]) => {
    if (Object.hasOwn(props.modelValue, name)) {
      return [name, props.modelValue[name] as string | number | boolean | null]
    }
    if (Object.hasOwn(field, 'default')) {
      return [name, field.default as string | number | boolean | null]
    }
    const options = enumValues(field)
    const firstOption = options?.[0]
    if (firstOption !== undefined) {
      return [name, schemaRequired(name) && !acceptsNull(field) ? '' : firstOption]
    }
    return [name, acceptsNull(field) ? null : scalarType(field) === 'boolean' ? false : '']
  }))
  emit('update:modelValue', values.value)
}

watch(() => props.schema, resetValues, { immediate: true })
watch(() => props.modelValue, (value) => { values.value = { ...value } }, { deep: true })

function acceptsNull(field: JsonSchema): boolean {
  return field.type === 'null' || field.anyOf?.some((option) => option.type === 'null') === true
}

function schemaRequired(name: string): boolean {
  return props.schema.required?.includes(name) === true
}

function scalarType(field: JsonSchema): string | undefined {
  return field.type ?? field.anyOf?.find((option) => option.type !== 'null')?.type
}

function inputType(field: JsonSchema): string {
  return field.writeOnly
    ? 'password'
    : scalarType(field) === 'number' || scalarType(field) === 'integer'
      ? 'number'
      : 'text'
}

function enumValues(field: JsonSchema): Array<string | number | boolean> | undefined {
  return field.enum ?? field.anyOf?.find((option) => option.type !== 'null')?.enum
}

function enumIndex(field: JsonSchema, value: string | number | boolean | null | undefined): string {
  if (value === null && acceptsNull(field)) return ''
  const index = enumValues(field)?.findIndex((option) => Object.is(option, value)) ?? -1
  return index < 0 ? '' : String(index)
}

function update(name: string, field: JsonSchema, event: Event): void {
  const input = event.target as HTMLInputElement | HTMLSelectElement
  const type = scalarType(field)
  const options = enumValues(field)
  const value: string | number | boolean | null = options
    ? (acceptsNull(field) && input.value === '' ? null : options[Number(input.value)] ?? '')
    : acceptsNull(field) && input.value.trim() === ''
      ? null
      : type === 'boolean'
        ? input instanceof HTMLInputElement && input.type === 'checkbox'
          ? input.checked
          : input.value === 'true'
        : type === 'number' || type === 'integer'
          ? input.value === '' ? '' : Number(input.value)
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
        v-if="enumValues(field)"
        :id="`config-${name}`"
        :value="enumIndex(field, values[name])"
        :disabled="disabled"
        :required="schemaRequired(name) && !acceptsNull(field)"
        @change="update(name, field, $event)"
      >
        <option v-if="schemaRequired(name) && !acceptsNull(field)" value="" disabled>Select…</option>
        <option v-else-if="acceptsNull(field)" value=""></option>
        <option v-for="(option, index) in enumValues(field)" :key="index" :value="index">{{ option }}</option>
      </select>
      <select
        v-else-if="scalarType(field) === 'boolean' && acceptsNull(field)"
        :id="`config-${name}`"
        :value="values[name] === null ? '' : String(values[name])"
        :disabled="disabled"
        @change="update(name, field, $event)"
      >
        <option value=""></option>
        <option value="true">Yes</option>
        <option value="false">No</option>
      </select>
      <input
        v-else-if="scalarType(field) === 'boolean'"
        :id="`config-${name}`"
        type="checkbox"
        :checked="Boolean(values[name])"
        :disabled="disabled"
        @change="update(name, field, $event)"
      >
      <input
        v-else
        :id="`config-${name}`"
        :type="inputType(field)"
        :value="values[name] ?? ''"
        :disabled="disabled"
        :required="schemaRequired(name) && !acceptsNull(field)"
        @input="update(name, field, $event)"
      >
      <p v-if="field.description" class="muted">{{ field.description }}</p>
    </div>
    <p v-if="properties.length === 0" class="muted">This provider does not declare configuration fields.</p>
    <slot />
  </form>
</template>

<style scoped>
.schema-form { display: grid; gap: var(--space-3); }
.schema-form p { margin: 0; }
.schema-form__field { display: grid; gap: var(--space-1); }
.schema-form__field > label { font-weight: 600; }
.schema-form input:not([type='checkbox']), .schema-form select { max-width: 32rem; padding: var(--space-2); }
</style>
