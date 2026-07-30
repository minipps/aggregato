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
  <section>
    <h1>Statistics</h1>
    <p class="muted">Archive activity is counted by logged entries, not by cover art or normalized scores.</p>

    <form class="filters" @submit.prevent="reload">
      <label for="stats-period">Group activity by</label>
      <select id="stats-period" v-model="period">
        <option value="all">All time</option>
        <option value="year">Year</option>
        <option value="month">Month</option>
        <option value="week">Week</option>
      </select>
      <label><input v-model="includeSubunits" type="checkbox" /> Include episode, track, and other sub-unit activity</label>
      <button type="submit">Refresh statistics</button>
    </form>

    <LoadingState v-if="summary.loading.value" label="Calculating archive statistics…" />
    <ErrorState v-else-if="summary.error.value" :problem="summary.error.value" retryable @retry="reload" />
    <template v-else-if="summary.data.value">
      <p class="stat-total"><strong>{{ summary.data.value.total_entries }}</strong> logged entries</p>
      <p v-if="!includeSubunits" class="muted">Sub-unit activity is excluded. Enable it above to include it.</p>
      <div class="stats-grid">
        <section aria-labelledby="type-heading">
          <h2 id="type-heading">By media type</h2>
          <dl class="pairs"><template v-for="[name, count] in mediaTypes" :key="name"><dt>{{ name }}</dt><dd>{{ count }}</dd></template></dl>
        </section>
        <section aria-labelledby="provider-heading">
          <h2 id="provider-heading">By provider</h2>
          <dl class="pairs"><template v-for="[name, count] in providers" :key="name"><dt>{{ name }}</dt><dd>{{ count }}</dd></template></dl>
        </section>
        <section aria-labelledby="period-heading">
          <h2 id="period-heading">Over time</h2>
          <dl class="pairs"><template v-for="bucket in summary.data.value.by_period" :key="bucket.period"><dt>{{ bucket.period }}</dt><dd>{{ bucket.count }}</dd></template></dl>
        </section>
      </div>
    </template>

    <section aria-labelledby="top-works-heading">
      <h2 id="top-works-heading">Most logged works</h2>
      <LoadingState v-if="works.loading.value" label="Loading top works…" />
      <ErrorState v-else-if="works.error.value" :problem="works.error.value" retryable @retry="works.reload()" />
      <ol v-else class="ranked"><li v-for="work in works.data.value?.items ?? []" :key="work.id"><RouterLink :to="{ name: 'work', params: { id: work.id } }">{{ work.label }}</RouterLink><span>{{ work.count }}</span></li></ol>
    </section>
    <section aria-labelledby="top-creators-heading">
      <h2 id="top-creators-heading">Most logged creators</h2>
      <LoadingState v-if="creators.loading.value" label="Loading top creators…" />
      <ErrorState v-else-if="creators.error.value" :problem="creators.error.value" retryable @retry="creators.reload()" />
      <ol v-else class="ranked"><li v-for="creator in creators.data.value?.items ?? []" :key="creator.id"><RouterLink :to="{ name: 'creator', params: { id: creator.id } }">{{ creator.label }}</RouterLink><span>{{ creator.count }}</span></li></ol>
    </section>
  </section>
</template>

<style scoped>
.stat-total { font-size: 1.25rem; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); gap: var(--space-4); }
.ranked { max-width: var(--measure); padding-left: var(--space-6); }
.ranked li { display: flex; justify-content: space-between; gap: var(--space-3); padding: var(--space-1) 0; }
</style>
