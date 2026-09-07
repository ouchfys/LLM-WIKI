<template>
  <header class="page-header">
    <div class="page-header-copy">
      <p class="page-kicker">PaperWiki · 工作台</p>
      <h1>{{ title }}</h1>
      <p class="page-description">{{ description }}</p>
    </div>
    <div v-if="$slots.default" class="page-header-actions"><slot /></div>
  </header>
</template>

<script setup lang="ts">
defineProps<{ title: string; description: string }>()
</script>
