<script setup lang="ts">
/**
 * Sign in: the API token is exchanged for a session cookie, once, and then forgotten.
 *
 * FR-032 forbids the token from appearing in a URL or in page source. It therefore travels in an
 * Authorization header (never a query string), is bound to a password field so it is not rendered,
 * and is cleared from memory as soon as the exchange succeeds. Nothing persists it client-side —
 * the HttpOnly cookie the server sets is the credential from then on.
 */

import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { login, toProblem } from '@/api/client'
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
  <section class="login">
    <h1>Sign in to Aggregato</h1>
    <p class="hint">
      Paste the API token from your configuration file. It is exchanged for a session cookie and not
      stored in the browser.
    </p>

    <form class="form" @submit.prevent="submit">
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
      <button type="submit" :disabled="submitting || token === ''">
        {{ submitting ? 'Signing in…' : 'Sign in' }}
      </button>
    </form>

    <div id="login-error">
      <!-- The problem's own message, not a status code: a wrong token says so in words (T058). -->
      <ErrorState v-if="problem" :problem="problem" />
    </div>
  </section>
</template>

<style scoped>
.login {
  max-width: 32rem;
}

.hint {
  color: var(--text-muted);
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

label {
  font-weight: 600;
}

input {
  font: inherit;
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text);
}

button {
  align-self: start;
}
</style>
