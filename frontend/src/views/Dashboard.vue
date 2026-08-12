<script setup lang="ts">
/**
 * Landing screen: the newest entries, and whether every provider is actually working.
 *
 * A degraded provider is named here by id rather than reduced to an overall badge — "degraded" with
 * no subject tells the operator nothing they can act on ( scenario 2). The two panels load
 * independently, so a failing `/health` still leaves the log readable (UI consistency guidance).
 */

import { computed } from 'vue'

import { entries, health } from '@/api/client'
import { useSyncStream } from '@/api/syncStream'
import { usePaged, useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import EntryList from '@/components/EntryList.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'
import ProviderStatus from '@/components/ProviderStatus.vue'
import SyncProgress from '@/components/SyncProgress.vue'

const RECENT_LIMIT = 10

// One cursor page is exactly what "recent" means here; there is no second page to walk.
const recent = usePaged(() => entries({ limit: RECENT_LIMIT }))
const status = useRequest(health)
const sync = useSyncStream()

const allProviders = computed(() => status.data.value?.providers ?? [])
const activeRuns = computed(() => sync.snapshot.value?.runs.filter((run) => run.status === 'running') ?? [])
const queuedProviders = computed(
  () => sync.snapshot.value?.providers.filter((provider) => provider.requested_mode !== null) ?? [],
)

const unhealthy = computed(() =>
  allProviders.value.filter(
    (provider) => provider.status === 'degraded' || provider.status === 'misconfigured',
  ),
)

function liveProvider(id: string | undefined) {
  return sync.snapshot.value?.providers.find((provider) => provider.id === id)
}
</script>

<template>
  <h1 class="visually-hidden">Aggregato dashboard</h1>

  <div class="bento">
    <!-- Featured tile: what this screen is, and the one link off it that most visits want. -->
    <section class="card card--accent span-2">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M4 19V5m0 14h16M8 15V9m4 6V6m4 9v-4" />
          </svg>
        </span>
        <h2>Aggregato</h2>
      </div>
      <p>Your logged activity from every platform, in one archive you own.</p>
      <p class="actions">
        <RouterLink :to="{ name: 'log' }">Browse the full log</RouterLink>
      </p>
    </section>

    <!-- Health in a sentence. The per-provider table below carries the detail. -->
    <section class="card card--feature span-2" aria-labelledby="health-heading">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M3 12h4l2-5 3 10 2-5h7" />
          </svg>
        </span>
        <h2 id="health-heading">Sync health</h2>
      </div>
      <LoadingState v-if="status.loading.value" label="Loading health…" />
      <ErrorState
        v-else-if="status.error.value"
        :problem="status.error.value"
        retryable
        @retry="status.reload()"
      />
      <template v-else-if="status.data.value">
        <!-- A degraded provider is named by id: "degraded" with no subject tells the operator
             nothing they can act on. -->
        <div v-if="unhealthy.length" class="note note--danger" role="alert">
          <p class="note__title">Needs attention</p>
          <p>{{ unhealthy.map((provider) => provider.id ?? 'an unknown provider').join(', ') }}</p>
          <p><RouterLink :to="{ name: 'providers' }">Review providers</RouterLink></p>
        </div>
        <p v-else class="note note--ok">Every provider is healthy.</p>
        <!-- The two counts live here rather than in tiles of their own: they only mean anything
             next to the sentence that interprets them. -->
        <dl class="pairs">
          <dt>Providers</dt>
          <dd>{{ allProviders.length }}</dd>
          <dt>Need attention</dt>
          <dd :class="{ 'count--warn': unhealthy.length > 0 }">{{ unhealthy.length }}</dd>
        </dl>
      </template>
    </section>

    <section class="card span-4" aria-labelledby="live-sync-heading">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M4 12a8 8 0 0 1 14-5l2 2m0-5v5h-5M20 12a8 8 0 0 1-14 5l-2-2m0 5v-5h5" />
          </svg>
        </span>
        <h2 id="live-sync-heading">Live sync activity</h2>
        <span v-if="sync.connected.value" class="badge badge--ok">live</span>
      </div>
      <div v-if="activeRuns.length" class="live-sync-list">
        <article v-for="run in activeRuns" :key="run.id" class="subsection">
          <h3>{{ run.provider_id }} · {{ run.mode }} sync</h3>
          <SyncProgress :run="run" />
        </article>
      </div>
      <div v-else-if="queuedProviders.length" class="note" role="status">
        <p>{{ queuedProviders.map((provider) => provider.id).join(', ') }} queued; waiting for the scheduler to start.</p>
      </div>
      <p v-else-if="sync.snapshot.value" class="note note--ok">No sync is running.</p>
      <p v-else class="muted" role="status">Connecting to live sync updates…</p>
      <p v-if="sync.error.value" class="muted">{{ sync.error.value.detail ?? sync.error.value.title }}</p>
    </section>

    <!-- Primary content, and the widest tile on the screen: the whole row, so a run of entries is
         readable without scrolling a narrow column. -->
    <section class="card card--scroll span-4" aria-labelledby="recent-heading">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M12 7v5l3 2" />
            <circle cx="12" cy="12" r="9" />
          </svg>
        </span>
        <h2 id="recent-heading">Recent entries</h2>
      </div>
      <ErrorState
        v-if="recent.error.value"
        :problem="recent.error.value"
        retryable
        @retry="recent.restart()"
      />
      <LoadingState v-else-if="recent.loading.value" label="Loading recent entries…" />
      <EntryList v-else-if="recent.items.value.length > 0" :entries="recent.items.value" />
      <EmptyState v-else title="Nothing logged yet" detail="Entries appear here after the first sync." />
    </section>

    <section class="card span-4" aria-labelledby="per-provider-heading">
      <h2 id="per-provider-heading">Per provider</h2>
      <table v-if="allProviders.length > 0">
        <caption class="visually-hidden">Per-provider health</caption>
        <thead>
          <tr>
            <th scope="col">Provider</th>
            <th scope="col">Status</th>
            <th scope="col">Last success</th>
            <th scope="col">Failures</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="provider in allProviders" :key="provider.id ?? ''">
            <th scope="row">
              <RouterLink :to="{ name: 'providers' }">{{ provider.id ?? 'unknown' }}</RouterLink>
            </th>
            <td>
              <ProviderStatus
                v-if="liveProvider(provider.id)?.status"
                :status="liveProvider(provider.id)!.status"
              />
              <ProviderStatus v-else-if="provider.status" :status="provider.status" />
              <span v-else>unknown</span>
            </td>
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
        v-else-if="!status.loading.value"
        title="No providers configured"
        detail="Enable one on the Providers screen to start building the archive."
      />
    </section>
  </div>
</template>

<style scoped>
.pairs {
  font-size: 1.05rem;
}

.pairs dd {
  font-weight: 700;
}

.count--warn {
  color: var(--warn);
}

/* Capped so the per-provider tile below stays on screen: the feed scrolls inside its own tile
   rather than pushing the rest of the dashboard past the fold. */
.card--scroll {
  max-height: 24rem;
}
</style>
