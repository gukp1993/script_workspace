<script setup lang="ts">
/**
 * 启动/暂停/恢复/停止控制条（UI-012 基础版）。
 *
 * - 模式明显区分：按钮旁展示当前会话模式的四色风险徽标；
 * - 停止永远可点（stopAlwaysEnabled 恒真），点击后展示清理进度文案；
 * - 按钮可用性由 sessionStore 的 state 派生。
 */
import { computed } from 'vue'

import { modeRisk } from '../domain/modes'
import { useSessionStore } from '../stores/session'

const session = useSessionStore()

const modeInfo = computed(() => (session.current ? modeRisk(session.current.mode) : null))

const stateLabel = computed(() => {
  switch (session.current?.state) {
    case 'created':
      return '已创建'
    case 'running':
      return '运行中'
    case 'paused':
      return '已暂停'
    case 'resumed':
      return '运行中'
    case 'stopped':
      return '已停止'
    case 'failed':
      return '失败'
    case 'interrupted':
      return '已中断'
    default:
      return '无会话'
  }
})
</script>

<template>
  <div class="control-bar">
    <template v-if="session.current">
      <span class="session-tag" :style="{ borderColor: modeInfo?.color }">
        <span class="dot" :style="{ background: modeInfo?.color }" />
        {{ modeInfo?.label }} · {{ session.current.session_id }}
      </span>
      <span class="state">{{ stateLabel }}</span>

      <button :disabled="!session.canStart" @click="session.start()">启动</button>
      <button :disabled="!session.canPause" @click="session.pause()">暂停</button>
      <button :disabled="!session.canResume" @click="session.resume()">恢复</button>
      <!-- 停止永远可点（UI-012） -->
      <button class="stop" :disabled="!session.stopAlwaysEnabled" @click="session.stop()">停止</button>

      <span v-if="session.cleaning" class="cleaning" role="status">{{ session.cleaning }}</span>
    </template>
    <template v-else>
      <span class="idle">无运行会话 — 在「目标选择」页创建会话后可控制</span>
      <button class="stop" :disabled="true" title="当前没有会话">停止</button>
    </template>
    <span v-if="session.busy" class="busy" aria-busy="true">处理中…</span>
  </div>
</template>

<style scoped>
.control-bar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.session-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid;
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 12px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.state {
  color: #94a3b8;
  font-size: 13px;
  min-width: 90px;
}
.stop {
  border-color: #ef4444;
  color: #fca5a5;
  font-weight: 600;
}
.cleaning {
  color: #fbbf24;
  font-size: 13px;
}
.idle {
  color: #64748b;
  font-size: 13px;
  margin-right: auto;
}
.busy {
  color: #64748b;
  font-size: 12px;
}
</style>
