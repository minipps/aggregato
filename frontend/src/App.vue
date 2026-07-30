<script setup lang="ts">
import { RouterView } from 'vue-router'
</script>

<template>
  <a class="skip-link" href="#main-content">Skip to content</a>
  <header class="site-header">
    <nav aria-label="Primary navigation">
      <RouterLink class="brand" :to="{ name: 'dashboard' }">Aggregato</RouterLink>
      <ul class="site-nav">
        <li><RouterLink :to="{ name: 'log' }">Log</RouterLink></li>
        <li><RouterLink :to="{ name: 'stats' }">Statistics</RouterLink></li>
        <li><RouterLink :to="{ name: 'creators' }">Creators</RouterLink></li>
        <li><RouterLink :to="{ name: 'providers' }">Providers</RouterLink></li>
        <li><RouterLink :to="{ name: 'sync-history' }">Sync history</RouterLink></li>
        <li><RouterLink :to="{ name: 'ingest-failures' }">Ingest failures</RouterLink></li>
        <li><RouterLink :to="{ name: 'resolution' }">Resolution</RouterLink></li>
        <li><RouterLink :to="{ name: 'settings' }">Settings</RouterLink></li>
      </ul>
    </nav>
  </header>
  <main id="main-content" tabindex="-1"><RouterView /></main>
</template>
