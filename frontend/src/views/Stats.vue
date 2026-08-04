<script setup lang="ts">
/** Archive-level activity statistics, deliberately text and data-table led rather than cover led. */
import { computed, ref, watch } from 'vue'

import { statsSummary, topStats } from '@/api/client'
import type { StatsPeriod } from '@/api/types'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import { useRequest } from '@/api/useApi'
import { calendarWeeks, type HeatmapCell } from './heatmap'

const period: StatsPeriod = 'day'
const includeSubunits = ref(false)
const summary = useRequest(() => statsSummary(period, includeSubunits.value))
const works = useRequest(() => topStats('work', period, includeSubunits.value))
const creators = useRequest(() => topStats('creator', period, includeSubunits.value))

const mediaTypes = computed(() => Object.entries(summary.data.value?.by_media_type ?? {}))
const providers = computed(() => Object.entries(summary.data.value?.by_provider ?? {}))

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

const counts = computed(
  () => new Map((summary.data.value?.by_period ?? []).map((b) => [b.period, b.count])),
)
/** Years with activity, newest first, so the picker never offers an empty grid. */
const availableYears = computed(() => {
  const found = new Set([...counts.value.keys()].map((day) => day.slice(0, 4)))
  return [...found].sort((a, b) => b.localeCompare(a))
})
const year = ref(String(new Date().getFullYear()))
watch(availableYears, (list) => {
  if (list.length && !list.includes(year.value)) year.value = list[0] as string
})

/** Days of the shown year only, so a quiet year still uses the full shade range. */
const shownDays = computed(() => [...counts.value].filter(([day]) => day.startsWith(year.value)))
const busiestDay = computed(() => Math.max(1, ...shownDays.value.map(([, count]) => count)))
const weeks = computed(() => calendarWeeks(year.value, counts.value, busiestDay.value))

const yearTotal = computed(() => shownDays.value.reduce((sum, [, count]) => sum + count, 0))
const activeDays = computed(() => shownDays.value.length)

/** Month label above the week column where that month first appears. */
function monthLabel(week: HeatmapCell[]): string {
  const first = week.find((cell) => cell?.day.endsWith('-01'))
  return first ? (MONTHS[Number(first.day.slice(5, 7)) - 1] ?? '') : ''
}

function reload(): void {
  void Promise.all([summary.reload(), works.reload(), creators.reload()])
}

watch(includeSubunits, reload)
</script>

<template>
  <div class="bento">
    <section class="card card--accent span-2">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M5 20V10m7 10V4m7 16v-7" />
          </svg>
        </span>
        <h1>Statistics</h1>
      </div>
      <p>Archive activity is counted by logged entries, not by cover art or normalized scores.</p>
    </section>

    <section class="card" aria-labelledby="total-label">
      <p id="total-label" class="metric-label">Logged entries</p>
      <LoadingState v-if="summary.loading.value" label="Counting…" />
      <p v-else class="metric">{{ summary.data.value?.total_entries ?? '—' }}</p>
    </section>

    <form class="card" @submit.prevent="reload">
      <h2 class="visually-hidden">Filters</h2>
      <label><input v-model="includeSubunits" type="checkbox" /> Include sub-unit activity</label>
      <button type="submit">Refresh</button>
    </form>

    <ErrorState
      v-if="summary.error.value"
      class="span-4"
      :problem="summary.error.value"
      retryable
      @retry="reload"
    />
    <template v-else-if="summary.data.value">
      <section class="card card--feature span-4 tall" aria-labelledby="period-heading">
        <div class="card__head">
          <span class="card__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M4 5h4v14H4zm6 0h4v9h-4zm6 0h4v6h-4z" />
            </svg>
          </span>
          <h2 id="period-heading">Activity</h2>
          <div class="field field--inline">
            <label class="visually-hidden" for="heatmap-year">Year</label>
            <select id="heatmap-year" v-model="year">
              <option v-for="option in availableYears" :key="option" :value="option">{{ option }}</option>
              <option v-if="!availableYears.includes(year)" :value="year">{{ year }}</option>
            </select>
          </div>
        </div>
        <p class="muted">{{ yearTotal }} entries across {{ activeDays }} active days in {{ year }}.</p>
        <div class="heatmap">
          <div class="heatmap__days" aria-hidden="true">
            <span>Mon</span><span>Wed</span><span>Fri</span><span>Sun</span>
          </div>
          <div class="heatmap__scroll">
            <div class="heatmap__months" aria-hidden="true">
              <span v-for="(week, index) in weeks" :key="index">{{ monthLabel(week) }}</span>
            </div>
            <div class="heatmap__grid">
              <template v-for="(week, index) in weeks" :key="index">
                <span
                  v-for="(cell, row) in week"
                  :key="cell?.day ?? `pad-${index}-${row}`"
                  class="heatmap__cell"
                  :class="cell ? `level-${cell.level}` : 'heatmap__cell--pad'"
                  :title="cell ? `${cell.day}: ${cell.count}` : undefined"
                />
              </template>
            </div>
          </div>
        </div>
        <p class="heatmap__legend">
          Less
          <span v-for="level in [0, 1, 2, 3, 4]" :key="level" :class="['heatmap__cell', `level-${level}`]" aria-hidden="true" />
          More
        </p>
      </section>

    </template>

    <section class="card card--feature span-2 tall" aria-labelledby="top-works-heading">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M8 21h8m-4-4v4M6 4h12v5a6 6 0 0 1-12 0V4Z" />
          </svg>
        </span>
        <h2 id="top-works-heading">Most logged works</h2>
      </div>
      <LoadingState v-if="works.loading.value" label="Loading top works…" />
      <ErrorState v-else-if="works.error.value" :problem="works.error.value" retryable @retry="works.reload()" />
      <ol v-else class="ranked"><li v-for="work in works.data.value?.items ?? []" :key="work.id"><RouterLink :to="{ name: 'work', params: { id: work.id } }">{{ work.label }}</RouterLink><span>{{ work.count }}</span></li></ol>
    </section>

    <section class="card card--feature span-2 tall" aria-labelledby="top-creators-heading">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <circle cx="12" cy="8" r="4" />
            <path d="M5 21a7 7 0 0 1 14 0" />
          </svg>
        </span>
        <h2 id="top-creators-heading">Most logged creators</h2>
      </div>
      <LoadingState v-if="creators.loading.value" label="Loading top creators…" />
      <ErrorState v-else-if="creators.error.value" :problem="creators.error.value" retryable @retry="creators.reload()" />
      <ol v-else class="ranked"><li v-for="creator in creators.data.value?.items ?? []" :key="creator.id"><RouterLink :to="{ name: 'creator', params: { id: creator.id } }">{{ creator.label }}</RouterLink><span>{{ creator.count }}</span></li></ol>
    </section>

    <template v-if="summary.data.value">
      <section class="card span-2" aria-labelledby="type-heading">
        <h2 id="type-heading">By media type</h2>
        <dl class="pairs"><template v-for="[name, count] in mediaTypes" :key="name"><dt>{{ name }}</dt><dd>{{ count }}</dd></template></dl>
        <p v-if="!includeSubunits" class="muted">Sub-unit activity is excluded. Enable it above to include it.</p>
      </section>
      <section class="card span-2" aria-labelledby="provider-heading">
        <h2 id="provider-heading">By provider</h2>
        <dl class="pairs"><template v-for="[name, count] in providers" :key="name"><dt>{{ name }}</dt><dd>{{ count }}</dd></template></dl>
      </section>
    </template>
  </div>
</template>

<style scoped>
/* Day heatmap: one square per day, seven rows (Mon–Sun) flowing into week columns. */
.heatmap { --cell: 0.85rem; --cell-gap: 3px; display: flex; gap: var(--space-2); }
.heatmap__scroll { overflow-x: auto; padding-bottom: var(--space-2); }
.heatmap__grid { display: grid; grid-auto-flow: column; grid-template-rows: repeat(7, var(--cell)); grid-auto-columns: var(--cell); gap: var(--cell-gap); }
.heatmap__months, .heatmap__days { display: grid; gap: var(--cell-gap); font-size: 0.7rem; color: var(--text-muted); }
.heatmap__months { grid-auto-flow: column; grid-auto-columns: var(--cell); margin-bottom: var(--cell-gap); }
/* Labels sit on rows 1/3/5/7 of the same seven-row rhythm as the grid, offset by the month strip. */
.heatmap__days { grid-template-rows: repeat(7, var(--cell)); margin-top: calc(1rem + var(--cell-gap)); text-align: right; }
.heatmap__days span:nth-child(2) { grid-row: 3; }
.heatmap__days span:nth-child(3) { grid-row: 5; }
.heatmap__days span:nth-child(4) { grid-row: 7; }
.heatmap__cell { border-radius: 3px; background: color-mix(in srgb, var(--accent) var(--tint, 0%), var(--ctp-surface1)); }
.heatmap__cell--pad { background: none; }
.level-1 { --tint: 25%; }
.level-2 { --tint: 50%; }
.level-3 { --tint: 75%; }
.level-4 { --tint: 100%; }
.heatmap__legend { display: flex; align-items: center; gap: var(--cell-gap, 3px); font-size: 0.7rem; color: var(--text-muted); }
.heatmap__legend .heatmap__cell { width: 0.85rem; height: 0.85rem; }
.heatmap__legend span:first-of-type { margin-left: var(--space-2); }
.field--inline { margin-left: auto; }

.ranked { margin: 0; padding-left: var(--space-6); }
.ranked li { display: flex; justify-content: space-between; gap: var(--space-3); padding: var(--space-1) 0; font-variant-numeric: tabular-nums; }
</style>
