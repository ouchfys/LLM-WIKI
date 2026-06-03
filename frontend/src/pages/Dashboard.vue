<template>
  <section>
    <section v-if="!onboarded" class="surface-panel onboarding-panel">
      <div>
        <p class="eyebrow">FIRST RUN PROFILE</p>
        <h2>先告诉贾维斯你是谁，要准备什么</h2>
        <p>
          这一步会建立冷启动画像。后续你的搜索、阅读、保存 Wiki、忽略推荐都会继续修正画像，
          推荐系统不会只靠这一次选择。
        </p>
      </div>

      <div class="onboarding-form">
        <div class="form-grid">
          <n-input v-model:value="profileDraft.target_role" placeholder="目标岗位，例如：大模型应用开发 / 算法工程师" />
          <n-select v-model:value="profileDraft.level" :options="levelOptions" placeholder="当前水平" />
        </div>
        <n-input v-model:value="profileDraft.learning_goal" placeholder="近期目标，例如：准备 RAG Agent 项目面试" />
        <n-input v-model:value="profileDraft.interests" placeholder="兴趣方向，用逗号分隔，例如：RAG评估, Agent记忆, 多模态RAG" />
        <n-input v-model:value="profileDraft.weak_points" placeholder="薄弱点，用逗号分隔，例如：rerank, RAG评估, 论文精读" />
        <n-select
          v-model:value="profileDraft.source_preferences"
          multiple
          :options="sourcePreferenceOptions"
          placeholder="偏好的内容来源"
        />
        <div class="form-grid">
          <n-input-number v-model:value="profileDraft.monthly_reading_target" :min="1" :max="60" placeholder="每月阅读目标" />
          <n-button type="primary" :loading="savingProfile" @click="saveOnboarding">保存基础画像</n-button>
        </div>
      </div>
    </section>

    <div class="jarvis-command">
      <div class="command-copy">
        <p class="eyebrow">PRIVATE JARVIS</p>
        <h2>把你读过、见过、想过的内容沉淀成面试可讲的知识资产</h2>
        <p>
          私有化贾维斯围绕「推荐阅读 -> 读完记录 -> Wiki 沉淀 -> 精读问答」运转。
          默认先做轻量 PaperIndex 和 Wiki，只有真正值得深挖的论文才进入精读流程。
        </p>
        <n-space>
          <n-button type="primary" @click="$router.push('/monthly-reads')">查看本月阅读</n-button>
          <n-button @click="$router.push('/capture')">记录一条知识</n-button>
        </n-space>
      </div>

      <div class="mission-panel">
        <p>今日建议</p>
        <h3>{{ nextAction.title }}</h3>
        <span>{{ nextAction.description }}</span>
      </div>
    </div>

    <div class="metric-grid">
      <div class="metric-card">
        <span>阅读候选</span>
        <strong>{{ total }}</strong>
        <p>本月待筛选内容</p>
      </div>
      <div v-for="item in statusCards" :key="item.label" class="metric-card">
        <span>{{ item.label }}</span>
        <strong>{{ item.value }}</strong>
        <p>{{ item.hint }}</p>
      </div>
    </div>

    <div class="dashboard-layout">
      <section class="surface-panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">QUEUE</p>
            <h3>最近推荐</h3>
          </div>
          <n-button text @click="$router.push('/monthly-reads')">进入队列</n-button>
        </div>

        <n-empty v-if="!monthlyItems.length" description="当前还没有本月阅读清单。">
          <template #extra>
            <n-button type="primary" :loading="seeding" @click="seedDemo">生成演示阅读清单</n-button>
          </template>
        </n-empty>

        <div v-else class="compact-list">
          <article v-for="item in monthlyItems.slice(0, 5)" :key="item.id" class="compact-item">
            <div>
              <h4>{{ item.title }}</h4>
              <p>{{ item.summary || '暂无摘要。' }}</p>
            </div>
            <n-tag size="small">{{ statusLabel(item.status) }}</n-tag>
          </article>
        </div>
      </section>

      <section class="surface-panel">
        <div class="panel-heading">
          <div>
            <p class="eyebrow">LOOP</p>
            <h3>系统闭环</h3>
          </div>
        </div>
        <div class="loop-list">
          <div class="loop-item">
            <b>1. 冷启动推荐</b>
            <span>用目标岗位、研究方向和薄弱点筛出本月阅读。</span>
          </div>
          <div class="loop-item">
            <b>2. 知识捕获</b>
            <span>论文笔记、公众号解读、小红书面经都可以沉淀到 Wiki。</span>
          </div>
          <div class="loop-item">
            <b>3. 精读升级</b>
            <span>值得深挖的论文进入 PaperIndex / RAG 问答，而不是所有内容都重入库。</span>
          </div>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, type ReadingItem } from '../api'

const monthlyItems = ref<ReadingItem[]>([])
const counts = ref<Record<string, number>>({})
const seeding = ref(false)
const onboarded = ref(true)
const savingProfile = ref(false)
const profileDraft = reactive({
  target_role: '',
  learning_goal: '',
  level: null as string | null,
  interests: '',
  weak_points: '',
  source_preferences: [] as string[],
  monthly_reading_target: 8
})

const levelOptions = [
  { label: '刚入门，需要更多综述和八股', value: 'beginner' },
  { label: '有项目基础，需要补论文和系统设计', value: 'project_based' },
  { label: '准备面试，需要高频追问和表达素材', value: 'interview_ready' },
  { label: '研究导向，需要跟踪前沿论文', value: 'research' }
]

const sourcePreferenceOptions = [
  { label: '优先一手论文', value: 'primary_papers' },
  { label: '需要中文解读', value: 'chinese_explainers' },
  { label: '偏好工程博客', value: 'engineering_blogs' },
  { label: '关注面经八股', value: 'interview_notes' },
  { label: '需要代码实现', value: 'code_repos' }
]

const total = computed(() => monthlyItems.value.length)
const statusCards = computed(() => [
  { label: '已入库', value: counts.value.saved || 0, hint: '已沉淀到阅读系统' },
  { label: '已读完', value: counts.value.read || 0, hint: '等待整理为表达材料' },
  { label: '待精读', value: counts.value.deep_read || 0, hint: '适合进入论文问答' }
])

const nextAction = computed(() => {
  if (!monthlyItems.value.length) {
    return {
      title: '先生成一份演示阅读队列',
      description: '用它检查推荐、笔记、Wiki 和精读入口是否形成闭环。'
    }
  }
  if ((counts.value.deep_read || 0) > 0) {
    return {
      title: '处理待精读论文',
      description: '优先把真正有价值的论文进入 PaperIndex，避免重型 RAG 拖慢日常使用。'
    }
  }
  return {
    title: '从一篇内容开始记录',
    description: '读完后写 takeaways、开放问题和面试可讲点，系统会继续积累你的画像。'
  }
})

async function load() {
  const [{ data: reads }, { data: profile }] = await Promise.all([
    api.get('/monthly-reads'),
    api.get('/profile')
  ])
  monthlyItems.value = reads.items
  counts.value = reads.counts
  onboarded.value = Boolean(profile.onboarded)
}

async function seedDemo() {
  seeding.value = true
  try {
    await api.post('/monthly-reads/seed-demo')
    await load()
  } finally {
    seeding.value = false
  }
}

async function saveOnboarding() {
  savingProfile.value = true
  try {
    await api.post('/profile/onboarding', {
      target_role: profileDraft.target_role,
      learning_goal: profileDraft.learning_goal,
      level: profileDraft.level || '',
      interests: splitComma(profileDraft.interests),
      weak_points: splitComma(profileDraft.weak_points),
      source_preferences: profileDraft.source_preferences,
      monthly_reading_target: profileDraft.monthly_reading_target || 8
    })
    await load()
  } finally {
    savingProfile.value = false
  }
}

function splitComma(text: string) {
  return text.split(/[,，]/).map((item) => item.trim()).filter(Boolean)
}

function statusLabel(status: string) {
  return {
    candidate: '候选',
    saved: '已入库',
    read: '已读',
    deep_read: '待精读',
    ignored: '已忽略'
  }[status] || status
}

onMounted(load)
</script>
