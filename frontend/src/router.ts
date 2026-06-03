import { createRouter, createWebHistory } from 'vue-router'

const ChatPage = () => import('./pages/Wiki.vue')
const CapturePage = () => import('./pages/CaptureNote.vue')
const DailyReadsPage = () => import('./pages/MonthlyReads.vue')
const KnowledgeVaultPage = () => import('./pages/KnowledgeVault.vue')
const EvaluationPage = () => import('./pages/EvaluationDashboard.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: ChatPage },
    { path: '/capture', component: CapturePage },
    { path: '/vault', component: KnowledgeVaultPage },
    { path: '/evaluation', component: EvaluationPage },
    { path: '/daily', component: DailyReadsPage },
    { path: '/monthly-reads', redirect: '/daily' },
    { path: '/wiki', redirect: '/' },
    { path: '/profile', redirect: '/vault' },
    { path: '/papers', redirect: '/vault' }
  ]
})

export default router
