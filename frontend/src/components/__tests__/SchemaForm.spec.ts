import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SchemaForm from '../SchemaForm.vue'

const schema = {
  description: 'Connect a provider account.',
  required: ['token', 'choice'],
  properties: {
    token: { title: 'Access token', type: 'string', writeOnly: true, description: 'Stored locally.' },
    choice: { title: 'Choice', type: 'integer', enum: [1, 2] },
    visibility: { title: 'Visibility', type: 'string', enum: ['public', 'private'], default: 'public' },
    include_notes: { title: 'Include notes', type: 'boolean', default: false },
    count: { title: 'Count', type: 'integer' },
    score: { title: 'Score', anyOf: [{ type: 'integer' }, { type: 'null' }], default: null },
    level: { title: 'Level', type: 'integer', enum: [1, 2], default: 2 },
    enabled: { title: 'Enabled', type: 'boolean', enum: [true, false], default: true },
    nullable_enabled: { anyOf: [{ type: 'boolean' }, { type: 'null' }], default: null },
    nullable_mode: {
      anyOf: [{ type: 'string', enum: ['fast', 'slow'] }, { type: 'null' }],
      default: null,
    },
    profile_url: {
      title: 'Profile URL',
      anyOf: [{ type: 'string', format: 'uri' }, { type: 'null' }],
      default: null,
    },
  },
}

describe('SchemaForm', () => {
  it('renders JSON Schema fields with secret and enum controls', () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    expect(wrapper.get('input[type="password"]').attributes('required')).toBeDefined()
    expect(wrapper.get('#config-visibility').findAll('option')).toHaveLength(2)
    expect(wrapper.text()).toContain('Stored locally.')
  })

  it('emits initial defaults and preserves an explicit null default', () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    const emitted = wrapper.emitted('update:modelValue')?.[0]?.[0]

    expect(emitted).toMatchObject({
      visibility: 'public',
      include_notes: false,
      level: 2,
      enabled: true,
      nullable_enabled: null,
      score: null,
      nullable_mode: null,
      profile_url: null,
    })
  })

  it('uses own model values and defaults even when hasOwnProperty is shadowed', () => {
    const modelValue = Object.create({ visibility: 'private' })
    modelValue.include_notes = true
    modelValue.hasOwnProperty = 'shadowed'
    const wrapper = mount(SchemaForm, {
      props: { schema, modelValue },
    })

    expect(wrapper.emitted('update:modelValue')?.[0]?.[0]).toMatchObject({
      visibility: 'public',
      include_notes: true,
      score: null,
    })
  })

  it('emits typed changes for a checkbox', async () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    await wrapper.get('input[type="checkbox"]').setValue(true)
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ include_notes: true })
  })

  it('emits null for an empty optional field', async () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    await wrapper.get('#config-profile_url').setValue('')
    expect(wrapper.emitted('update:modelValue')?.[0]?.[0]).toMatchObject({ profile_url: null })
  })

  it('reads nullable numeric schemas as numbers and emits null for a blank value', async () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    const score = wrapper.get<HTMLInputElement>('#config-score')

    expect(score.attributes('type')).toBe('number')
    await score.setValue('7')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ score: 7 })
    await score.setValue('')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ score: null })
  })

  it('preserves blank numbers and typed enum values', async () => {
    const wrapper = mount(SchemaForm, { props: { schema } })

    await wrapper.get('#config-count').setValue('')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ count: '' })
    await wrapper.get('#config-level').setValue('0')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ level: 1 })
    await wrapper.get('#config-enabled').setValue('1')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ enabled: false })
    await wrapper.get('#config-nullable_mode').setValue('0')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ nullable_mode: 'fast' })
    await wrapper.get('#config-nullable_mode').setValue('')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ nullable_mode: null })
    await wrapper.get('#config-nullable_enabled').setValue('true')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ nullable_enabled: true })
    await wrapper.get('#config-nullable_enabled').setValue('')
    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toMatchObject({ nullable_enabled: null })
  })

  it('keeps browser validation on required fields', async () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    const form = wrapper.get('form').element as HTMLFormElement

    expect(form.checkValidity()).toBe(false)
    await wrapper.get('#config-token').setValue('token')
    expect(form.checkValidity()).toBe(false)
    await wrapper.get('#config-choice').setValue('0')
    expect(form.checkValidity()).toBe(true)
  })

  it('emits submit so a parent can persist schema-driven settings', async () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')).toHaveLength(1)
  })
})
