<template>
  <details class="chat-prompts" :open="expanded">
    <summary>从一个问题开始 <span>选择后可编辑，再发送</span></summary>
    <div class="prompt-list">
      <button v-for="prompt in prompts" :key="prompt" type="button" @click="$emit('select', prompt)">
        {{ prompt }}
      </button>
    </div>
  </details>
</template>

<script setup lang="ts">
defineProps<{ expanded: boolean }>()
defineEmits<{ select: [prompt: string] }>()
const prompts = [
  '对比 DPO 和 RLHF，并引用知识库中的来源。',
  '解释 Self-Attention 与 Multi-Head Attention 的区别。',
  'QServe 如何降低量化推理的开销？'
]
</script>

<style scoped>
.chat-prompts { padding: 4px; flex-shrink: 0; }
summary { color: var(--text-soft); cursor: pointer; font-size: 13px; }
summary span { margin-left: 10px; color: var(--text-muted); font-size: 12px; }
.prompt-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
button {
  min-height: 40px;
  padding: 9px 12px;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: var(--text-soft);
  font-size: 13px;
  line-height: 1.5;
  cursor: pointer;
  transition: background-color 160ms ease, border-color 160ms ease;
}
button:hover { background: var(--accent-soft); border-color: var(--accent); }
</style>
