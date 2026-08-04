/** Calendar layout for the activity heatmap, kept out of the view so the date arithmetic is testable. */

/** A day square keyed by ISO date; `null` pads a partial week so weekday rows stay aligned. */
export type HeatmapCell = { day: string; count: number; level: number } | null

function isoDay(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

/** Split one calendar year into Monday-start week columns of seven day cells. */
export function calendarWeeks(
  year: string,
  counts: Map<string, number>,
  busiest: number,
): HeatmapCell[][] {
  const start = new Date(`${year}-01-01T00:00:00`)
  const end = new Date(`${year}-12-31T00:00:00`)
  const columns: HeatmapCell[][] = []
  // Monday = 0, so a year starting mid-week gets the right number of leading blanks.
  let column: HeatmapCell[] = Array<HeatmapCell>((start.getDay() + 6) % 7).fill(null)
  for (const cursor = new Date(start); cursor <= end; cursor.setDate(cursor.getDate() + 1)) {
    const day = isoDay(cursor)
    const count = counts.get(day) ?? 0
    column.push({ day, count, level: count ? Math.ceil((count / busiest) * 4) : 0 })
    if (column.length === 7) {
      columns.push(column)
      column = []
    }
  }
  if (column.length) columns.push([...column, ...Array<HeatmapCell>(7 - column.length).fill(null)])
  return columns
}
