<script setup lang="ts">
/** Archive-level activity statistics, deliberately text and data-table led rather than cover led. */
import { computed, ref, watch } from 'vue'

import { statsSummary, topStats } from '@/api/client'
import type { StatsPeriod } from '@/api/types'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import { useRequest } from '@/api/useApi'

const period = ref<StatsPeriod>('all')
const includeSubunits = ref(false)
const summary = useRequest(() => statsSummary(period.value, includeSubunits.value))
const works = useRequest(() => topStats('work', period.value, includeSubunits.value))
const creators = useRequest(() => topStats('creator', period.value, includeSubunits.value))

const mediaTypes = computed(() => Object.entries(summary.data.value?.by_media_type ?? {}))
const providers = computed(() => Object.entries(summary.data.value?.by_provider ?? {}))

function reload(): void {
  void Promise.all([summary.reload(), works.reload(), creators.reload()])
}

watch([period, includeSubunits], reload)
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
      <div class="field">
        <label for="stats-period">Group activity by</label>
        <select id="stats-period" v-model="period">
          <option value="all">All time</option>
          <option value="year">Year</option>
          <option value="month">Month</option>
          <option value="week">Week</option>
        </select>
      </div>
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
      <section class="card span-2" aria-labelledby="type-heading">
        <h2 id="type-heading">By media type</h2>
        <dl class="pairs"><template v-for="[name, count] in mediaTypes" :key="name"><dt>{{ name }}</dt><dd>{{ count }}</dd></template></dl>
        <p v-if="!includeSubunits" class="muted">Sub-unit activity is excluded. Enable it above to include it.</p>
      </section>
      <section class="card" aria-labelledby="provider-heading">
        <h2 id="provider-heading">By provider</h2>
        <dl class="pairs"><template v-for="[name, count] in providers" :key="name"><dt>{{ name }}</dt><dd>{{ count }}</dd></template></dl>
      </section>
      <section class="card card--scroll" aria-labelledby="period-heading">
        <h2 id="period-heading">Over time</h2>
        <dl class="pairs"><template v-for="bucket in summary.data.value.by_period" :key="bucket.period"><dt>{{ bucket.period }}</dt><dd>{{ bucket.count }}</dd></template></dl>
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
  </div>
</template>

<style scoped>
.ranked { margin: 0; padding-left: var(--space-6); }
.ranked li { display: flex; justify-content: space-between; gap: var(--space-3); padding: var(--space-1) 0; font-variant-numeric: tabular-nums; }
</style>
