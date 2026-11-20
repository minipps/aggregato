import { createApp } from 'vue'

import App from './App.vue'
import { router } from './router'
// Self-hosted and bundled by Vite: the UI never fetches a font from a third party. Only the weight
// axis is loaded; the width axis is unused.
import '@fontsource-variable/instrument-sans/wght.css'
import './styles/main.css'

createApp(App).use(router).mount('#app')
