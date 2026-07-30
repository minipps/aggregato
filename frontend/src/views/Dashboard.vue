<script setup lang="ts">
/**
 * Landing screen: the newest entries, and whether every provider is actually working.
 *
 * A degraded provider is named here by id rather than reduced to an overall badge — "degraded" with
 * no subject tells the operator nothing they can act on (US3 scenario 2). The two panels load
 * independently, so a failing `/health` still leaves the log readable (Constitution III).
 */

import { computed } from 'vue'

import { entries, health } from '@/api/client'
import { usePaged, useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import EntryList from '@/components/EntryList.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'
import ProviderStatus from '@/components/ProviderStatus.vue'

const RECENT_LIMIT = 10

// One cursor page is exactly what "recent" means here; there is no second page to walk.
const recent = usePaged(() => entries({ limit: RECENT_LIMIT }))
const status = useRequest(health)

const unhealthy = computed(() =>
  (status.data.value?.providers ?? []).filter(
    (provider) => provider.status === 'degraded' || provider.status === 'misconfigured',
  ),
)
</script>

<template>
  <section>
    <h1>Aggregato</h1>

    <h2>Provider health</h2>
    <LoadingState v-if="status.loading.value" label="Loading health…" />
    <ErrorState
      v-else-if="status.error.value"
      :problem="status.error.value"
      retryable
      @retry="status.reload()"
    />
    <template v-else-if="status.data.value">
      <div v-if="unhealthy.length" class="degraded-banner" role="alert">
        Sync needs attention for {{ unhealthy.map((provider) => provider.id ?? 'an unknown provider').join(', ') }}.
        <RouterLink :to="{ name: 'providers' }">Review providers</RouterLink>
      </div>
      <p :class="['summary', status.data.value.status === 'ok' ? 'summary--ok' : 'summary--warn']">
        <template v-if="unhealthy.length === 0">Every provider is healthy.</template>
        <template v-else>
          Needs attention:
          <strong>{{ unhealthy.map((provider) => provider.id ?? 'unknown provider').join(', ') }}</strong>
        </template>
      </p>

      <table v-if="status.data.value.providers.length > 0">
        <caption class="visually-hidden">Per-provider health</caption>
        <thead>
          <tr>
            <th scope="col">Provider</th>
            <th scope="col">Status</th>
            <th scope="col">Last success</th>
            <th scope="col">Consecutive failures</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="provider in status.data.value.providers" :key="provider.id ?? ''">
            <th scope="row">
              <RouterLink :to="{ name: 'providers' }">{{ provider.id ?? 'unknown' }}</RouterLink>
            </th>
            <td><ProviderStatus v-if="provider.status" :status="provider.status" /><span v-else>unknown</span></td>
            <td>
              <LoggedAt
                v-if="provider.last_success_at"
                :at="provider.last_success_at"
                precision="exact"
              />
              <span v-else class="muted">Never</span>
            </td>
            <td>{{ provider.consecutive_failures ?? 0 }}</td>
          </tr>
        </tbody>
      </table>
      <EmptyState
        v-else
        title="No providers configured"
        detail="Enable one on the Providers screen to start building the archive."
      />
    </template>

    <h2>Recent entries</h2>
    <ErrorState
      v-if="recent.error.value"
      :problem="recent.error.value"
      retryable
      @retry="recent.restart()"
    />
    <LoadingState v-else-if="recent.loading.value" label="Loading recent entries…" />
    <EntryList v-else-if="recent.items.value.length > 0" :entries="recent.items.value" />
    <EmptyState v-else title="Nothing logged yet" detail="Entries appear here after the first sync." />

    <p>
      <RouterLink :to="{ name: 'log' }">Browse the full log</RouterLink>
    </p>
  </section>
</template>

<style scoped>
.summary {
  border-left: 4px solid var(--ok);
  padding-left: var(--space-3);
}

.summary--warn {
  border-left-color: var(--warn);
}

.degraded-banner { border: 1px solid var(--danger); color: var(--danger); padding: var(--space-3); margin-bottom: var(--space-3); }
</style>
