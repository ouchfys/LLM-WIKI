<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="themeOverrides">
    <n-message-provider>
    <div class="app-shell">
      <div class="desktop-shell">
        <aside class="app-sidebar">
          <div class="sidebar-top">
            <div class="brand-lockup">
              <img class="brand-logo" :src="logoUrl" alt="PaperWiki" />
              <div class="brand-copy">
                <strong>PaperWiki</strong>
          <span>有据可查的个人知识库</span>
              </div>
            </div>

            <n-button class="new-chat-button" type="primary" @click="startNewChat">
              新建对话
            </n-button>
          </div>

          <section class="sidebar-section">
            <p class="sidebar-label">工作台</p>
            <nav class="sidebar-nav" aria-label="主导航">
              <button
                v-for="item in navItems"
                :key="item.path"
                class="sidebar-nav-item"
                :class="{ active: isActive(item.path) }"
                :aria-current="isActive(item.path) ? 'page' : undefined"
                :title="item.hint"
                type="button"
                @click="go(item.path)"
              >
                <span class="nav-dot"></span>
                <div>
                  <strong>{{ item.label }}</strong>
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
                  :disabled="deletingSessions"
                  @click="clearAllSessions"
                >
                  清空记录
                </button>
              </div>
            </div>

            <p v-if="sessionNotice" class="session-notice" :class="{ error: sessionNoticeError }" role="status">
              {{ sessionNotice }}
            </p>
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
    </n-message-provider>
  </n-config-provider>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { darkTheme, NButton, NConfigProvider, NMessageProvider, type GlobalThemeOverrides } from 'naive-ui'
import { api, apiErrorMessage } from './api'

type ChatSession = {
  id: string
  title: string
  created_at: string
}

const route = useRoute()
const router = useRouter()
const recentSessions = ref<ChatSession[]>([])
const sessionNotice = ref('')
const sessionNoticeError = ref(false)
const deletingSessions = ref(false)
const logoUrl = new URL('./assets/logo-ui.png', import.meta.url).href

const navItems = [
  { path: '/', label: '对话', hint: '用个人知识库回答问题' },
  { path: '/capture', label: '资料导入', hint: '论文与社媒资料编译入库' },
  { path: '/vault', label: '知识库', hint: '论文卡片与概念网络' },
  { path: '/evaluation', label: '质量评测', hint: '查看回答质量与失败样例' },
  { path: '/reviews', label: '冲突审批', hint: '查看冲突来源与结论对象' },
]

const themeOverrides: GlobalThemeOverrides = {
  common: {
    textColorBase: '#eff1eb',
    textColor1: '#eff1eb',
    textColor2: '#c8cec5',
    textColor3: '#9ea79e',
    bodyColor: '#0b0908',
    primaryColor: '#9bb8ad',
    primaryColorHover: '#d4e3d8',
    primaryColorPressed: '#8fa99e',
    successColor: '#22c55e',
    warningColor: '#f59e0b',
    errorColor: '#f43f5e',
    infoColor: '#adcabe',
    borderRadius: '10px',
    borderRadiusSmall: '8px',
    fontFamily: '"Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif',
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
    textColor: '#eff1eb',
    placeholderColor: '#929991',
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
        textColor: '#eff1eb',
        border: '1px solid rgba(195, 214, 202, 0.14)',
        borderHover: '1px solid rgba(195, 214, 202, 0.34)',
        borderFocus: '1px solid rgba(195, 214, 202, 0.7)'
      }
    }
  },
  Tabs: {
    tabTextColorBar: '#94a3b8',
    tabTextColorActiveBar: '#eff1eb',
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
    sessionNoticeError.value = false
    sessionNotice.value = '已删除该会话及其全部消息。'
    if (activeSessionId.value === sessionId) {
      localStorage.removeItem('wiki_chat_session_id')
      await router.push({
        path: '/',
        query: { new: String(Date.now()) }
      })
    }
    await loadSessions()
  } catch (error) {
    sessionNoticeError.value = true
    sessionNotice.value = apiErrorMessage(error, '删除失败，记录尚未清理，请重试。')
    console.error('[App] failed to delete session:', error)
  }
}

async function clearAllSessions() {
  if (deletingSessions.value) return
  const ok = window.confirm('清空所有会话记录？长期记忆、用户画像和个人 Wiki 会保留。')
  if (!ok) return

  try {
    deletingSessions.value = true
    const { data } = await api.delete<{ deleted: number }>('/wiki/sessions')
    sessionNoticeError.value = false
    sessionNotice.value = '已从数据库删除 ' + data.deleted + ' 个会话及其消息。Wiki 和长期记忆已保留。'
    localStorage.removeItem('wiki_chat_session_id')
    recentSessions.value = []
    await router.push({
      path: '/',
      query: { new: String(Date.now()) }
    })
  } catch (error) {
    sessionNoticeError.value = true
    sessionNotice.value = apiErrorMessage(error, '清空失败，记录尚未清理，请重试。')
    console.error('[App] failed to clear sessions:', error)
  } finally {
    deletingSessions.value = false
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
