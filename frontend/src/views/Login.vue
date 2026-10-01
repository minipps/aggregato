<script setup lang="ts">
/**
 * Sign in: the API token is exchanged for a session cookie, once, and then forgotten.
 *
 * The token is sent in an Authorization header. After the exchange, the server's HttpOnly session
 * cookie is used for API requests.
 */

import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { login, toProblem } from '@/api/client'
import { loadSession } from '@/api/session'
import type { Problem } from '@/api/types'
import ErrorState from '@/components/ErrorState.vue'

const route = useRoute()
const router = useRouter()

const token = ref('')
const submitting = ref(false)
const problem = ref<Problem | undefined>(undefined)

async function submit(): Promise<void> {
  submitting.value = true
  problem.value = undefined
  try {
    await login(token.value)
    token.value = ''
    // The read-only token signs in here too, so what this session may do is only known now.
    await loadSession()
    const next = route.query['next']
    await router.replace(typeof next === 'string' && next.startsWith('/') ? next : '/')
  } catch (caught) {
    problem.value = toProblem(caught)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="bento login">
    <section class="card card--accent span-2">
      <div class="card__head">
        <span class="card__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <rect x="4" y="10" width="16" height="10" rx="2" />
            <path d="M8 10V7a4 4 0 0 1 8 0v3" />
          </svg>
        </span>
        <h1>Sign in to Aggregato</h1>
      </div>
      <p>
        Paste the API token from your configuration file. It is exchanged for a session cookie and
        not stored in the browser.
      </p>
      <p>
        A read-only token signs in the same way and browses the archive without being able to change
        it.
      </p>
    </section>

    <form class="card span-2" @submit.prevent="submit">
      <h2 class="visually-hidden">Credentials</h2>
      <div class="field">
        <label for="api-token">API token</label>
        <input
          id="api-token"
          v-model="token"
          type="password"
          name="token"
          autocomplete="current-password"
          required
          :aria-describedby="problem ? 'login-error' : undefined"
        />
      </div>
      <p class="actions">
        <button type="submit" :disabled="submitting || token === ''">
          {{ submitting ? 'Signing in…' : 'Sign in' }}
        </button>
      </p>

      <div id="login-error">
        <!-- The problem detail gives a readable explanation when sign-in fails. -->
        <ErrorState v-if="problem" :problem="problem" />
      </div>
    </form>
  </div>
</template>

<style scoped>
/* Sign-in is one task, so the grid stays narrow rather than spreading two tiles across the page. */
.login {
  max-width: 52rem;
}

.field > label {
  font-weight: 600;
  color: var(--text);
}
</style>
