<script setup lang="ts">
/**
 * Work detail: identifiers, credits, entries, opinions, parent and sibling seasons.
 *
 * Ratings show raw value, scale, and normalized side by side, and each normalized value carries the
 * caveat in the reader's line of sight : normalization makes values comparable *within* a
 * scale, and states nothing about how one platform's 4/5 relates to another's 80/100. Documenting
 * that in the contract is not enough — the person reading the number is who needs to know.
 */

import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { mergeWork, undoMerge, work } from '@/api/client'
import { readonlyAccess } from '@/api/session'
import type { Rating } from '@/api/types'
import { useRequest } from '@/api/useApi'
import EmptyState from '@/components/EmptyState.vue'
import EntryList from '@/components/EntryList.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import LoggedAt from '@/components/LoggedAt.vue'
import MergeDialog from '@/components/MergeDialog.vue'

const route = useRoute()
const workId = computed(() => String(route.params['id'] ?? ''))

const detail = useRequest(() => work(workId.value))
watch(workId, () => void detail.reload())
const merging = ref(false)
const notice = ref('')

async function merge(loserIds: string[]): Promise<void> {
  const log = await mergeWork(workId.value, loserIds)
  notice.value = 'Works merged.'
  merging.value = false
  await detail.reload()
  window.setTimeout(() => { notice.value = `Merged. Undo: ${log.id}` }, 0)
}

async function undo(id: number): Promise<void> { await undoMerge(id); notice.value = 'Merge undone.'; await detail.reload() }

function ratingText(rating: Rating | null | undefined): string {
  if (!rating || rating.raw === null || rating.raw === undefined) return 'No rating'
  return rating.scale_id === null || rating.scale_id === undefined
    ? String(rating.raw)
    : `${rating.raw} on ${rating.scale_id}`
}
</script>

<template>
  <section>
    <LoadingState v-if="detail.loading.value" label="Loading work…" />
    <ErrorState
      v-else-if="detail.error.value"
      :problem="detail.error.value"
      retryable
      @retry="detail.reload()"
    />

    <div v-else-if="detail.data.value" class="bento">
      <section class="card card--feature span-2 tall">
        <!--
          Local cache path only  — the API never hands the browser a platform URL. alt is
          empty because the h1 beside it already names the work; a poster carries nothing a reader
          needs that the title does not already say.
        -->
        <img
          v-if="detail.data.value.image"
          class="poster"
          :src="detail.data.value.image"
          alt=""
          width="120"
          height="180"
          decoding="async"
        />
        <h1>{{ detail.data.value.title }}</h1>
        <p class="muted">
          {{ detail.data.value.media_type }} · {{ detail.data.value.media_family }}
          <template v-if="detail.data.value.release_year"> · {{ detail.data.value.release_year }}</template>
          <template v-if="detail.data.value.original_title">
            · originally {{ detail.data.value.original_title }}
          </template>
        </p>

        <p v-if="detail.data.value.parent">
          Part of
          <RouterLink :to="{ name: 'work', params: { id: detail.data.value.parent.id } }">
            {{ detail.data.value.parent.title }}
          </RouterLink>
        </p>

        <p v-if="!readonlyAccess" class="actions">
          <button type="button" @click="merging = true">Merge duplicate</button>
        </p>
        <MergeDialog v-if="merging" subject="work" :winner-id="workId" @merge="merge" @cancel="merging = false" />
        <p v-if="notice" class="note" role="status">
          {{ notice }}
          <button v-if="notice.includes('Undo:')" type="button" @click="undo(Number(notice.split(': ')[1]))">Undo</button>
        </p>
      </section>

      <section class="card card--scroll span-2 tall" aria-labelledby="entries-heading">
        <div class="card__head">
          <span class="card__icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M12 7v5l3 2" />
              <circle cx="12" cy="12" r="9" />
            </svg>
          </span>
          <h2 id="entries-heading">Entries</h2>
        </div>
        <EntryList
          v-if="detail.data.value.entries?.length"
          :entries="detail.data.value.entries"
          :show-work="false"
        />
        <EmptyState v-else title="No entries" detail="Nothing has been logged against this work." />
      </section>

      <!-- Opinions stay in one tile, split by hairlines: a card per provider would nest cards. -->
      <section class="card card--scroll span-2" aria-labelledby="opinions-heading">
        <h2 id="opinions-heading">Opinions</h2>
        <!-- Stated once, next to the numbers it qualifies . -->
        <p class="note note--warn">
          Normalized scores are comparable <strong>within one platform's scale</strong>. They are not
          equivalent across platforms: two providers' normalized 80s do not mean the same thing.
        </p>
        <template v-if="detail.data.value.opinions?.length">
          <article v-for="opinion in detail.data.value.opinions" :key="opinion.id" class="subsection opinion">
            <h3>{{ opinion.provider_id }}</h3>
            <dl class="pairs">
              <dt>Rating</dt>
              <dd>{{ ratingText(opinion.rating) }}</dd>
              <dt>Normalized</dt>
              <dd>
                <template v-if="opinion.rating?.normalized !== null && opinion.rating?.normalized !== undefined">
                  {{ opinion.rating.normalized }}/100
                  <span class="muted">(within {{ opinion.rating.scale_id ?? 'this scale' }} only)</span>
                </template>
                <span v-else class="muted">Not normalized</span>
              </dd>
              <template v-if="opinion.is_liked !== null && opinion.is_liked !== undefined">
                <dt>Liked</dt>
                <dd>{{ opinion.is_liked ? 'Yes' : 'No' }}</dd>
              </template>
              <template v-if="opinion.authored_at">
                <dt>Written</dt>
                <dd><LoggedAt :at="opinion.authored_at" precision="exact" /></dd>
              </template>
            </dl>
            <details v-if="opinion.review_text">
              <summary>Review{{ opinion.contains_spoilers ? ' (contains spoilers)' : '' }}</summary>
              <!-- Plain text only: provider markup is never injected as HTML. -->
              <p class="review">{{ opinion.review_text }}</p>
            </details>
          </article>
        </template>
        <EmptyState v-else title="No opinions" detail="No provider recorded a rating or review here." />
      </section>

      <section class="card span-2" aria-labelledby="credits-heading">
        <h2 id="credits-heading">Credits</h2>
        <ul v-if="detail.data.value.credits?.length" class="plain">
          <li v-for="credit in detail.data.value.credits" :key="credit.id">
            <strong>{{ credit.creator_name ?? credit.creator_id }}</strong>
            — {{ credit.role }}
            <span v-if="credit.credited_as" class="muted">(as {{ credit.credited_as }})</span>
            <span class="muted"> · linked by {{ credit.link_confidence }}</span>
          </li>
        </ul>
        <EmptyState v-else title="No credits" detail="No provider supplied credits for this work." />
      </section>

      <section class="card span-2" aria-labelledby="identifiers-heading">
        <h2 id="identifiers-heading">Identifiers</h2>
        <table v-if="detail.data.value.external_ids?.length">
          <caption class="visually-hidden">External identifiers for this work</caption>
          <thead>
            <tr>
              <th scope="col">Namespace</th>
              <th scope="col">Value</th>
              <th scope="col">Source</th>
              <th scope="col">Confidence</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="id in detail.data.value.external_ids" :key="`${id.namespace}:${id.value}`">
              <td>{{ id.namespace }}</td>
              <td class="mono">{{ id.value }}</td>
              <td>{{ id.source }}</td>
              <td>{{ id.confidence }}</td>
            </tr>
          </tbody>
        </table>
        <EmptyState v-else title="No identifiers" detail="Nothing to match this work by yet." />
      </section>

      <section v-if="detail.data.value.siblings?.length" class="card span-2" aria-labelledby="siblings-heading">
        <h2 id="siblings-heading">Other seasons</h2>
        <ul class="plain">
          <li v-for="sibling in detail.data.value.siblings" :key="sibling.id">
            <RouterLink :to="{ name: 'work', params: { id: sibling.id } }">
              {{ sibling.title }}
            </RouterLink>
            <span v-if="sibling.sequence_number" class="muted"> · #{{ sibling.sequence_number }}</span>
          </li>
        </ul>
      </section>
    </div>
  </section>
</template>

<style scoped>
/* Block above the title rather than floated: a float here would bleed past the header into the
   rest of the tile on a narrow screen. */
.poster {
  display: block;
  width: 120px;
  height: 180px;
  object-fit: cover;
  border-radius: var(--radius);
  background: var(--surface-raised);
}

.pairs {
  grid-template-columns: max-content minmax(0, 1fr);
}

.opinion {
  display: grid;
  gap: var(--space-2);
}

.review {
  white-space: pre-wrap;
}
</style>
