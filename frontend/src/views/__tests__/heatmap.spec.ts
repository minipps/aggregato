import { describe, expect, it } from 'vitest'

import { calendarWeeks } from '../heatmap'

describe('calendarWeeks', () => {
  it('lays a year out as aligned Monday-start weeks', () => {
    // 2021-01-01 was a Friday, so the first column carries four blanks before it.
    const weeks = calendarWeeks('2021', new Map(), 1)
    expect(weeks[0]?.slice(0, 4)).toEqual([null, null, null, null])
    expect(weeks[0]?.[4]?.day).toBe('2021-01-01')
    expect(weeks.every((week) => week.length === 7)).toBe(true)
    expect(weeks.flat().filter(Boolean)).toHaveLength(365)
    expect(calendarWeeks('2024', new Map(), 1).flat().filter(Boolean)).toHaveLength(366)
  })

  it('scales levels 1..4 against the busiest day and leaves empty days at 0', () => {
    const counts = new Map([
      ['2021-01-01', 1],
      ['2021-01-02', 10],
    ])
    const days = new Map(
      calendarWeeks('2021', counts, 10)
        .flat()
        .filter((cell) => cell !== null)
        .map((cell) => [cell.day, cell.level]),
    )
    expect(days.get('2021-01-01')).toBe(1)
    expect(days.get('2021-01-02')).toBe(4)
    expect(days.get('2021-01-03')).toBe(0)
  })
})
