<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterView } from 'vue-router'

import { loadSession, readonlyAccess } from '@/api/session'

// Once per page load: which credential this browser holds decides whether any write control is
// rendered at all. Views read the flag; nothing else has to thread it through.
onMounted(() => void loadSession())
</script>

<template>
  <a class="skip-link" href="#main-content">Skip to content</a>
  <header class="site-header">
    <nav aria-label="Primary navigation">
      <RouterLink class="brand" :to="{ name: 'dashboard' }">
        <!-- The mark is the layout itself: four tiles of a bento grid, one of them wide. -->
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
          <rect x="1" y="1" width="11" height="7" rx="2" />
          <rect x="14" y="1" width="5" height="7" rx="2" opacity="0.55" />
          <rect x="1" y="10" width="5" height="9" rx="2" opacity="0.55" />
          <rect x="8" y="10" width="11" height="9" rx="2" />
        </svg>
        Aggregato
      </RouterLink>
      <ul class="site-nav">
        <li><RouterLink :to="{ name: 'log' }">Log</RouterLink></li>
        <li><RouterLink :to="{ name: 'media' }">Media</RouterLink></li>
        <li><RouterLink :to="{ name: 'stats' }">Statistics</RouterLink></li>
        <li><RouterLink :to="{ name: 'creators' }">Creators</RouterLink></li>
        <li><RouterLink :to="{ name: 'providers' }">Providers</RouterLink></li>
        <li><RouterLink :to="{ name: 'sync-history' }">Sync history</RouterLink></li>
        <li v-if="readonlyAccess === false"><RouterLink :to="{ name: 'ingest-failures' }">Ingest failures</RouterLink></li>
        <li v-if="readonlyAccess === false"><RouterLink :to="{ name: 'resolution' }">Resolution</RouterLink></li>
        <li><RouterLink :to="{ name: 'settings' }">Settings</RouterLink></li>
      </ul>
      <!-- Said once, in the header, rather than as an explanation next to every missing button. -->
      <p v-if="readonlyAccess !== false" class="readonly-badge badge badge--warn">
        {{ readonlyAccess === true ? 'Read-only access' : 'Checking access…' }}
      </p>
    </nav>
  </header>
  <main id="main-content" tabindex="-1"><RouterView /></main>
</template>

<style scoped>
.readonly-badge {
  margin: 0;
  margin-left: auto;
}
</style>
