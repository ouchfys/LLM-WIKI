<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="themeOverrides">
    <div class="app-shell">
      <div class="ambient-light-layer" aria-hidden="true">
        <div class="ambient-blob ambient-blob-a"></div>
        <div class="ambient-blob ambient-blob-b"></div>
      </div>
      <div class="desktop-shell">
        <aside class="app-sidebar">
          <div class="sidebar-top">
            <div class="brand-lockup">
              <img class="brand-logo" :src="logoUrl" alt="笔记贾维斯" />
              <div class="brand-copy">
                <strong>笔记贾维斯</strong>
                <span>Private Research OS</span>
              </div>
            </div>

            <n-button class="new-chat-button" type="primary" @click="startNewChat">
              新建对话
            </n-button>
          </div>

          <section class="sidebar-section">
            <p class="sidebar-label">工作台</p>
            <nav class="sidebar-nav" aria-label="Primary">
              <button
                v-for="item in navItems"
                :key="item.path"
                class="sidebar-nav-item"
                :class="{ active: isActive(item.path) }"
                type="button"
                @click="go(item.path)"
              >
                <span class="nav-dot"></span>
                <div>
                  <strong>{{ item.label }}</strong>
                  <span>{{ item.hint }}</span>
                </div>
              </button>
            </nav>
          </section>

          <section class="sidebar-section grow">
            <div class="sidebar-section-head">
              <p class="sidebar-label">最近会话</p>
              <div class="sidebar-actions">
                <button type="button" class="subtle-action" @click="loadSessions">刷新</button>
                <button
                  v-if="recentSessions.length"
                  type="button"
                  class="subtle-action danger"
                  @click="clearAllSessions"
                >
                  清空
                </button>
              </div>
            </div>

            <div v-if="recentSessions.length" class="session-list">
              <div
                v-for="session in recentSessions"
                :key="session.id"
                class="session-item"
                :class="{ active: activeSessionId === session.id && route.path === '/' }"
              >
                <button type="button" class="session-open" @click="openSession(session.id)">
                  <strong>{{ displaySessionTitle(session.title) }}</strong>
                  <span>{{ formatSessionTime(session.created_at) }}</span>
                </button>
                <button
                  type="button"
                  class="session-delete"
                  title="删除会话记录"
                  @click="deleteSession(session.id)"
                >
                  删除
                </button>
              </div>
            </div>
            <div v-else class="empty-side-note">暂无历史会话</div>
          </section>
        </aside>

        <main class="app-workspace">
          <router-view />
        </main>
      </div>
    </div>
  </n-config-provider>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { darkTheme, NButton, NConfigProvider, type GlobalThemeOverrides } from 'naive-ui'
import { api } from './api'

type ChatSession = {
  id: string
  title: string
  created_at: string
}

const route = useRoute()
const router = useRouter()
const recentSessions = ref<ChatSession[]>([])
const logoUrl = new URL('./assets/logo-ui.png', import.meta.url).href

const navItems = [
  { path: '/', label: '对话', hint: '用个人知识库回答问题' },
  { path: '/capture', label: '采集台', hint: '论文、小红书、截图入库' },
  { path: '/vault', label: '知识库', hint: '论文卡片与概念网络' },
  { path: '/evaluation', label: '评测', hint: 'Benchmark 与失败样例' },
  { path: '/daily', label: '推荐', hint: '每日论文与项目线索' }
]

const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#9bb8ad',
    primaryColorHover: '#d4e3d8',
    primaryColorPressed: '#8fa99e',
    successColor: '#22c55e',
    warningColor: '#f59e0b',
    errorColor: '#f43f5e',
    infoColor: '#adcabe',
    borderRadius: '10px',
    borderRadiusSmall: '8px',
    fontFamily: '"Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif',
    fontWeightStrong: '650'
  },
  Card: {
    color: '#15130f',
    colorModal: '#15130f',
    borderColor: 'rgba(195, 214, 202, 0.16)',
    borderRadius: '14px'
  },
  Input: {
    color: '#11100d',
    colorFocus: '#11100d',
    colorDisabled: '#12100d',
    textColor: '#f8fafc',
    placeholderColor: '#64748b',
    border: '1px solid rgba(195, 214, 202, 0.14)',
    borderHover: '1px solid rgba(195, 214, 202, 0.34)',
    borderFocus: '1px solid rgba(195, 214, 202, 0.7)',
    boxShadowFocus: '0 0 0 3px rgba(155, 184, 173, 0.16)',
    caretColor: '#d4e3d8'
  },
  Select: {
    peers: {
      InternalSelection: {
        color: '#11100d',
        textColor: '#f8fafc',
        border: '1px solid rgba(195, 214, 202, 0.14)',
        borderHover: '1px solid rgba(195, 214, 202, 0.34)',
        borderFocus: '1px solid rgba(195, 214, 202, 0.7)'
      }
    }
  },
  Tabs: {
    tabTextColorBar: '#94a3b8',
    tabTextColorActiveBar: '#f8fafc',
    tabTextColorHoverBar: '#d4e3d8',
    barColor: '#9bb8ad'
  },
  Button: {
    borderRadiusSmall: '8px',
    borderRadiusMedium: '10px',
    borderRadiusLarge: '12px'
  },
  Tag: {
    borderRadius: '8px'
  },
  Modal: {
    color: '#15130f',
    borderRadius: '14px'
  }
}

const activeSessionId = computed(() => {
  if (route.path !== '/') return ''
  const routeSession = typeof route.query.session === 'string' ? route.query.session : ''
  return routeSession || localStorage.getItem('wiki_chat_session_id') || ''
})

function isActive(path: string) {
  return route.path === path
}

async function loadSessions() {
  try {
    const { data } = await api.get('/wiki/sessions')
    recentSessions.value = (data.items || []).slice(0, 10)
  } catch (error) {
    console.error('[App] failed to load sessions:', error)
  }
}

async function deleteSession(sessionId: string) {
  const ok = window.confirm('删除这条会话记录？长期记忆和个人 Wiki 不会被删除。')
  if (!ok) return

  try {
    await api.delete(`/wiki/sessions/${sessionId}`)
    if (activeSessionId.value === sessionId) {
      localStorage.removeItem('wiki_chat_session_id')
      await router.push({
        path: '/',
        query: { new: String(Date.now()) }
      })
    }
    await loadSessions()
  } catch (error) {
    console.error('[App] failed to delete session:', error)
  }
}

async function clearAllSessions() {
  const ok = window.confirm('清空所有会话记录？长期记忆、用户画像和个人 Wiki 会保留。')
  if (!ok) return

  try {
    await api.delete('/wiki/sessions')
    localStorage.removeItem('wiki_chat_session_id')
    recentSessions.value = []
    await router.push({
      path: '/',
      query: { new: String(Date.now()) }
    })
  } catch (error) {
    console.error('[App] failed to clear sessions:', error)
  }
}

function go(path: string) {
  if (route.path !== path) {
    router.push(path)
  }
}

function openSession(sessionId: string) {
  router.push({
    path: '/',
    query: { session: sessionId }
  })
}

function startNewChat() {
  router.push({
    path: '/',
    query: { new: String(Date.now()) }
  })
}

function formatSessionTime(value: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString('zh-CN', {
    month: 'numeric',
    day: 'numeric'
  })
}

function displaySessionTitle(value: string) {
  const title = (value || '').trim()
  if (!title) return '未命名会话'
  if (/^[?\uFFFD\s]+$/.test(title)) return '历史会话'
  return title
}

watch(
  () => route.fullPath,
  () => {
    loadSessions()
  }
)

onMounted(loadSessions)
</script>
