<template>
  <section>
    <div class="page-heading">
      <div>
        <p class="eyebrow">PERSONAL PROFILE</p>
        <h2>用户画像</h2>
        <span>基础画像负责冷启动，后续阅读、搜索、保存和忽略行为会持续修正推荐方向。</span>
      </div>
    </div>

    <div class="profile-layout">
      <section class="surface-panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">BASELINE</p>
            <h3>基础画像</h3>
          </div>
          <n-tag :type="onboarded ? 'success' : 'warning'">{{ onboarded ? '已完成' : '待设置' }}</n-tag>
        </div>

        <div class="profile-kv">
          <div>
            <span>目标岗位</span>
            <b>{{ preferences.target_role || '未设置' }}</b>
          </div>
          <div>
            <span>当前水平</span>
            <b>{{ levelLabel(preferences.learning_level) }}</b>
          </div>
          <div>
            <span>每月阅读目标</span>
            <b>{{ preferences.monthly_reading_target || '8' }} 篇</b>
          </div>
          <div>
            <span>来源偏好</span>
            <b>{{ sourcePreferenceText || '未设置' }}</b>
          </div>
        </div>
      </section>

      <section class="surface-panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">GOALS</p>
            <h3>学习目标</h3>
          </div>
        </div>
        <n-input v-model:value="goalsText" type="textarea" placeholder="每行一个目标，例如：准备 RAG 项目面试" />
        <n-button type="primary" class="action" @click="saveGoals">保存目标</n-button>
      </section>
    </div>

    <div class="profile-layout section-card">
      <section class="surface-panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">SIGNALS</p>
            <h3>添加画像信号</h3>
          </div>
        </div>
        <div class="form-grid">
          <n-select v-model:value="signalType" :options="signalOptions" />
          <n-input v-model:value="signalValue" placeholder="例如：RAG 评估、Agent 记忆、GraphRAG..." />
        </div>
        <n-button class="action" @click="addSignal">添加信号</n-button>
      </section>

      <section class="surface-panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">FEEDBACK</p>
            <h3>行为反馈</h3>
          </div>
        </div>
        <n-empty v-if="!feedback.length" description="还没有推荐反馈。保存、忽略、标记精读都会逐渐影响画像。" />
        <div v-else class="compact-list">
          <article v-for="item in feedback.slice(0, 5)" :key="item.id" class="compact-item">
            <div>
              <h4>{{ item.metadata?.title || item.item_id }}</h4>
              <p>{{ feedbackLabel(item.action) }}</p>
            </div>
            <n-tag size="small">{{ item.item_type || 'item' }}</n-tag>
          </article>
        </div>
      </section>
    </div>

    <section class="surface-panel section-card">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">MEMORY</p>
          <h3>画像信号</h3>
        </div>
      </div>
      <n-empty v-if="!signals.length" description="还没有画像信号。完成基础画像或添加兴趣点后，推荐会更贴近你的目标。" />
      <n-space v-else>
        <n-tag v-for="signal in signals" :key="signal.id" size="small">
          {{ signalTypeLabel(signal.signal_type) }} / {{ signal.value }}
        </n-tag>
      </n-space>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const goalsText = ref('')
const signalType = ref('interest')
const signalValue = ref('')
const signals = ref<any[]>([])
const feedback = ref<any[]>([])
const preferences = ref<Record<string, string>>({})
const onboarded = ref(false)

const signalOptions = [
  { label: '兴趣方向', value: 'interest' },
  { label: '薄弱点', value: 'weak_point' },
  { label: '偏好', value: 'preference' }
]

const sourcePreferenceText = computed(() => {
  return (preferences.value.source_preferences || '')
    .split('\n')
    .filter(Boolean)
    .map(sourcePreferenceLabel)
    .join(' / ')
})

async function load() {
  const { data } = await api.get('/profile')
  goalsText.value = (data.goals || []).join('\n')
  signals.value = data.signals || []
  feedback.value = data.feedback || []
  preferences.value = data.preferences || {}
  onboarded.value = Boolean(data.onboarded)
}

async function saveGoals() {
  await api.post('/profile/goals', { goals: goalsText.value.split('\n').filter(Boolean) })
  await load()
}

async function addSignal() {
  if (!signalValue.value.trim()) return
  await api.post('/profile/signals', {
    signal_type: signalType.value,
    key: 'topic',
    value: signalValue.value,
    weight: 1.5
  })
  signalValue.value = ''
  await load()
}

function signalTypeLabel(value: string) {
  return {
    interest: '兴趣',
    weak_point: '薄弱点',
    preference: '偏好',
    goal: '目标'
  }[value] || value
}

function levelLabel(value: string) {
  return {
    beginner: '刚入门',
    project_based: '有项目基础',
    interview_ready: '准备面试',
    research: '研究导向'
  }[value] || '未设置'
}

function sourcePreferenceLabel(value: string) {
  return {
    primary_papers: '一手论文',
    chinese_explainers: '中文解读',
    engineering_blogs: '工程博客',
    interview_notes: '面经八股',
    code_repos: '代码实现'
  }[value] || value
}

function feedbackLabel(value: string) {
  return {
    saved_to_wiki: '已存入 Wiki，增强相似主题兴趣',
    saved: '已入库',
    read: '已读完',
    deep_read: '标记为待精读',
    ignored: '已忽略，降低相似主题权重'
  }[value] || value
}

onMounted(load)
</script>
