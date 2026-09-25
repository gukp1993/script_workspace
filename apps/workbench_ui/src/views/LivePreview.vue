<script setup lang="ts">
/**
 * 实时画面预览（UI-004 基础版）。
 *
 * 轮询 /preview/shot（M1 主屏，max_width 缩放省带宽）；显示帧率与
 * 采集状态；错误态可重试。预览与控制条解耦：预览卡顿/停止不影响
 * 急停与停止按钮。
 */
import { onBeforeUnmount, onMounted } from 'vue'

import { usePreviewStore } from '../stores/preview'

const preview = usePreviewStore()

onMounted(() => {
  preview.startPolling()
})

onBeforeUnmount(() => {
  preview.stopPolling()
})
</script>

<template>
  <section class="live-preview">
    <header class="bar">
      <h2>实时预览</h2>
      <span class="status" :data-status="preview.status">
        {{ preview.statusLabel }}
      </span>
      <span class="meta">宽 ≤ {{ preview.maxWidth ?? '原始' }} px · 帧 #{{ preview.frameCount }}</span>
      <div class="actions">
        <button v-if="!preview.isStreaming" @click="preview.startPolling()">开启预览</button>
        <button v-else @click="preview.stopPolling()">暂停预览</button>
        <button v-if="preview.status === 'error'" @click="preview.clearError()">清除错误</button>
      </div>
    </header>

    <div class="frame">
      <img v-if="preview.frameUrl" :src="preview.frameUrl" alt="屏幕预览帧" />
      <div v-else class="placeholder">
        <p>尚未收到预览帧</p>
        <p class="hint">点击「开启预览」开始轮询控制面 /preview/shot</p>
      </div>
    </div>

    <p v-if="preview.error" class="error" role="alert">采集失败：{{ preview.error }}</p>
  </section>
</template>

<style scoped>
.live-preview {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bar {
  display: flex;
  align-items: center;
  gap: 14px;
}
h2 {
  margin: 0;
  font-size: 18px;
}
.status {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid #334155;
}
.status[data-status='streaming'] {
  border-color: #10b981;
  color: #6ee7b7;
}
.status[data-status='error'] {
  border-color: #ef4444;
  color: #fca5a5;
}
.meta {
  color: #64748b;
  font-size: 12px;
}
.actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
}
.frame {
  background: #020617;
  border: 1px solid #1e293b;
  border-radius: 8px;
  min-height: 320px;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.frame img {
  max-width: 100%;
  display: block;
}
.placeholder {
  color: #475569;
  text-align: center;
}
.hint {
  font-size: 12px;
}
.error {
  margin: 0;
  color: #f87171;
  font-size: 13px;
}
</style>
