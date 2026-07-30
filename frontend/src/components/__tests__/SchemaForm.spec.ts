import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SchemaForm from '../SchemaForm.vue'

const schema = {
  description: 'Connect a provider account.',
  required: ['token'],
  properties: {
    token: { title: 'Access token', type: 'string', writeOnly: true, description: 'Stored locally.' },
    visibility: { title: 'Visibility', type: 'string', enum: ['public', 'private'], default: 'public' },
    include_notes: { title: 'Include notes', type: 'boolean', default: false },
  },
}

describe('SchemaForm', () => {
  it('renders JSON Schema fields with secret and enum controls', () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    expect(wrapper.get('input[type="password"]').attributes('required')).toBeDefined()
    expect(wrapper.get('select').findAll('option')).toHaveLength(2)
    expect(wrapper.text()).toContain('Stored locally.')
  })

  it('emits typed changes for a checkbox', async () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    await wrapper.get('input[type="checkbox"]').setValue(true)
    expect(wrapper.emitted('update:modelValue')?.[0]?.[0]).toMatchObject({ include_notes: true })
  })

  it('emits submit so a parent can persist schema-driven settings', async () => {
    const wrapper = mount(SchemaForm, { props: { schema } })
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')).toHaveLength(1)
  })
})
