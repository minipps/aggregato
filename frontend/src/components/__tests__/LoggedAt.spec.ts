import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import LoggedAt from '../LoggedAt.vue'
import type { LoggedPrecision } from '@/api/types'

function render(at: string | null, precision: LoggedPrecision) {
  return mount(LoggedAt, { props: { at, precision } })
}

/** Any 12:34, 12.34, or 12:34:56 in the output — the shapes a locale renders a clock time as. */
const CLOCK = /\d{1,2}[:.]\d{2}/

describe('LoggedAt', () => {
  it('renders date and time for exact precision', () => {
    const wrapper = render('2026-03-14T21:45:00+00:00', 'exact')
    expect(wrapper.find('time').attributes('datetime')).toBe('2026-03-14T21:45:00.000Z')
    expect(wrapper.text()).toMatch(CLOCK)
  })

  it('never renders a time for day precision', () => {
    const wrapper = render('2026-03-14T00:00:00+00:00', 'day')
    expect(wrapper.find('time').attributes('datetime')).toBe('2026-03-14')
    expect(wrapper.text()).not.toMatch(CLOCK)
  })

  // : the platform knew a month. Rendering a clock would state something it never recorded.
  it('never renders a month-only date as a time', () => {
    const wrapper = render('2026-03-01T00:00:00+00:00', 'month')
    const time = wrapper.find('time')
    expect(time.attributes('datetime')).toBe('2026-03')
    expect(wrapper.text()).not.toMatch(CLOCK)
    expect(wrapper.text()).not.toContain('1')
    expect(wrapper.text()).toContain('2026')
  })

  it('renders the year alone for year precision', () => {
    const wrapper = render('2026-06-30T12:00:00+00:00', 'year')
    expect(wrapper.find('time').attributes('datetime')).toBe('2026')
    expect(wrapper.text()).toBe('2026')
  })

  it('says the date is unknown and emits no time element', () => {
    const wrapper = render('2026-06-30T12:00:00+00:00', 'unknown')
    expect(wrapper.find('time').exists()).toBe(false)
    expect(wrapper.text()).toBe('Date unknown')
  })

  it('does not invent a date when the timestamp is unusable', () => {
    expect(render(null, 'day').find('time').exists()).toBe(false)
    expect(render('not-a-date', 'exact').text()).toBe('Date unknown')
  })

  it('keeps the recorded day regardless of the viewer timezone', () => {
    // A late-evening UTC day-precision value must not slide to the 15th for a viewer east of UTC,
    // nor to the 13th for one west of it.
    expect(render('2026-03-14T23:30:00+00:00', 'day').find('time').attributes('datetime')).toBe(
      '2026-03-14',
    )
  })
})
