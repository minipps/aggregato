<script setup lang="ts">
/**
 * The one date renderer. Every surface that shows a logged date uses this component; ad-hoc
 * formatting elsewhere is forbidden by Constitution III (one date/time rendering rule).
 *
 * FR-004: the UI must never present more precision than the platform recorded. So precision decides
 * both the text and the `datetime` attribute:
 *
 *   exact   → date and time            datetime = the timestamp as received
 *   day     → date only, no time       datetime = YYYY-MM-DD
 *   month   → month and year           datetime = YYYY-MM
 *   year    → year                     datetime = YYYY
 *   unknown → "Date unknown", no <time> element at all
 *
 * Reduced precisions are formatted from the calendar fields exactly as recorded, with no timezone
 * conversion: reinterpreting a day-precision value in the viewer's zone can shift it to the
 * neighbouring day, which is fabricating a date the platform never stated. Only `exact` denotes a
 * real instant, and that one is rendered in the viewer's locale and zone.
 */

import { computed } from 'vue'

import type { LoggedPrecision } from '@/api/types'

const props = defineProps<{
  /** ISO 8601 timestamp from the API (`Entry.logged_at`). */
  at: string | null | undefined
  precision: LoggedPrecision
}>()

const UNKNOWN_TEXT = 'Date unknown'

/** Leading calendar fields of the ISO string, read textually — see the component docstring. */
const parts = computed(() => {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(props.at ?? '')
  const [, year, month, day] = match ?? []
  if (year === undefined || month === undefined || day === undefined) return undefined
  return { year, month, day }
})

function format(options: Intl.DateTimeFormatOptions, year: string, month: string, day: string) {
  // UTC noon: formatting is pinned to the recorded fields, and midday cannot be pushed over a date
  // boundary by the formatter's own arithmetic.
  const instant = new Date(`${year}-${month}-${day}T12:00:00Z`)
  return new Intl.DateTimeFormat(undefined, { ...options, timeZone: 'UTC' }).format(instant)
}

const rendered = computed<{ text: string; datetime?: string }>(() => {
  const field = parts.value
  if (props.precision === 'unknown' || field === undefined) return { text: UNKNOWN_TEXT }

  const { year, month, day } = field
  switch (props.precision) {
    case 'exact': {
      const instant = new Date(props.at ?? '')
      if (Number.isNaN(instant.getTime())) return { text: UNKNOWN_TEXT }
      return {
        text: new Intl.DateTimeFormat(undefined, {
          dateStyle: 'long',
          timeStyle: 'short',
        }).format(instant),
        datetime: instant.toISOString(),
      }
    }
    case 'day':
      return {
        text: format({ dateStyle: 'long' }, year, month, day),
        datetime: `${year}-${month}-${day}`,
      }
    case 'month':
      return {
        text: format({ year: 'numeric', month: 'long' }, year, month, day),
        datetime: `${year}-${month}`,
      }
    case 'year':
      return { text: year, datetime: year }
  }
})
</script>

<template>
  <!-- No <time> when the date is unknown: a machine-readable date would be an invention (FR-004). -->
  <span v-if="rendered.datetime === undefined" class="logged-at logged-at--unknown">
    {{ rendered.text }}
  </span>
  <time v-else class="logged-at" :datetime="rendered.datetime">{{ rendered.text }}</time>
</template>

<style scoped>
.logged-at--unknown {
  color: var(--text-muted);
  font-style: italic;
}
</style>
