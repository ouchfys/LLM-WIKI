import { createRouter, createWebHistory } from 'vue-router'

const ChatPage = () => import('./pages/Wiki.vue')
const CapturePage = () => import('./pages/CaptureNote.vue')
const KnowledgeVaultPage = () => import('./pages/KnowledgeVault.vue')
const ReviewCenterPage = () => import('./pages/ReviewCenter.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: ChatPage },
    { path: '/capture', component: CapturePage },
    { path: '/vault', component: KnowledgeVaultPage },
    { path: '/evaluation', redirect: '/' },
    { path: '/reviews', component: ReviewCenterPage },
    { path: '/daily', redirect: '/vault' },
    { path: '/monthly-reads', redirect: '/vault' },
    { path: '/wiki', redirect: '/' },
    { path: '/profile', redirect: '/vault' },
    { path: '/papers', redirect: '/vault' }
  ]
})

export default router
