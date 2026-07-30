import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

// Views are code-split: an operator lands on one screen, and pulling the whole app down to render
// it is the wrong trade on a single-board host. Routes are added by the story that builds the view.
const routes: RouteRecordRaw[] = [
  { path: '/', name: 'dashboard', component: () => import('@/views/Dashboard.vue') },
  { path: '/login', name: 'login', component: () => import('@/views/Login.vue') },
  { path: '/log', name: 'log', component: () => import('@/views/Log.vue') },
  { path: '/works/:id', name: 'work', component: () => import('@/views/Work.vue') },
  { path: '/providers', name: 'providers', component: () => import('@/views/Providers.vue') },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
