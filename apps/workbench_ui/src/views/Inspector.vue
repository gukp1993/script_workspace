<script setup lang="ts">
/**
 * 会话检视器（UI-013）。
 *
 * - 订阅控制面 WS 事件流（inspector store：last_seq 续传 + 自动重连）；
 * - 展示：最近感知字段表、当前状态 + 进入时长、预算余量、最近策略拒绝
 *   原因、已执行动作计数；
 * - Dry/Shadow/Real 模式色带：颜色 + 文字标签 + 图标（不只靠颜色）；
 * - 无运行会话时显示空态说明。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { modeRisk } from '../domain/modes'
import { useInspectorStore } from '../stores/inspector'
import { useSessionStore } from '../stores/session'

const inspector = useInspectorStore()
const session = useSessionStore()

/** 每秒跳动的时钟（驱动"已进入 Xs"刷新） */
const nowTick = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null

const modeInfo = computed(() => modeRisk(inspector.mode))

/** 模式色带（色块 + 文字 + 图标，Dry/Shadow/Real 明显区分） */
const modeBand = computed(() => ({
  background: `${modeInfo.value.color}22`,
  borderColor: modeInfo.value.color,
}))

const enteredSeconds = computed(() => {
  if (!inspector.stateEnteredAt) return null
  return Math.max(0, Math.round((nowTick.value - inspector.stateEnteredAt) / 1000))
})

const budgetLabel = computed(() => {
  if (inspector.budgetRemainingMs === null) return '未知（等待 executed 事件）'
  return `${(inspector.budgetRemainingMs / 1000).toFixed(1)} s`
})

const wsLabel = computed(() => {
  switch (inspector.wsState) {
    case 'open':
      return '已连接'
    case 'connecting':
      return '连接中…'
    case 'resync':
      return '重新同步中…'
    default:
      return '未连接'
  }
})

function startIfSession(): void {
  if (session.current) {
    inspector.bindSession(String(session.current.session_id), String(session.current.mode))
    inspector.start()
  }
}

watch(
  () => session.current?.session_id,
  () => startIfSession(),
)

onMounted(() => {
  startIfSession()
  timer = setInterval(() => {
    nowTick.value = Date.now()
  }, 1000)
})

onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer)
  inspector.stop()
})

function formatValue(value: unknown): string {
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4)
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (value === null || value === undefined) return '(空)'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
</script>

<template>
  <section class="inspector" aria-label="会话检视器">
    <header class="bar">
      <h2>会话检视器</h2>
      <span class="ws" :data-state="inspector.wsState">{{ wsLabel }}</span>
      <button v-if="session.current" @click="inspector.clearError()">清除提示</button>
    </header>

    <p v-if="!session.current" class="empty">
      暂无会话 — 在「目标选择」页创建会话后，此处实时显示感知/状态/策略/预算
    </p>

    <template v-else>
      <!-- 模式色带（Dry/Shadow/Real/Observe 明显区分：色块+文字+图标） -->
      <div class="mode-band" :style="modeBand" role="status">
        <span class="dot" :style="{ background: modeInfo.color }" aria-hidden="true" />
        <strong>{{ modeInfo.label }}模式</strong>
        <span class="mode-desc">{{ modeInfo.description }}</span>
        <span class="mode-meta">{{ inspector.sessionId }}</span>
      </div>

      <div class="cards">
        <div class="card">
          <h3>当前状态</h3>
          <p class="big">{{ inspector.currentState ?? '（等待 state_transition 事件）' }}</p>
          <p class="meta">
            <template v-if="enteredSeconds !== null">已进入 {{ enteredSeconds }} 秒</template>
            <template v-else>尚未收到状态事件</template>
            <template v-if="inspector.stateEnteredSeq !== null"> · seq #{{ inspector.stateEnteredSeq }}</template>
          </p>
        </div>

        <div class="card">
          <h3>预算余量</h3>
          <p class="big">{{ budgetLabel }}</p>
          <p class="meta">每帧检测预算（executed 事件 budget_remaining_ms）</p>
        </div>

        <div class="card">
          <h3>执行计数</h3>
          <p class="big">{{ inspector.executedCount }}</p>
          <p class="meta">本会话 executed 事件数</p>
        </div>
      </div>

      <div class="cards">
        <div class="card wide">
          <h3>最近感知字段</h3>
          <p v-if="inspector.fields.length === 0" class="meta">尚未收到 perception_snapshot 事件</p>
          <table v-else class="fields">
            <thead>
              <tr>
                <th>字段</th>
                <th>最近值</th>
                <th>seq</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="field in inspector.fields" :key="field.name">
                <td>{{ field.name }}</td>
                <td>{{ formatValue(field.value) }}</td>
                <td>#{{ field.seq }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="card wide">
          <h3>最近策略拒绝</h3>
          <p v-if="!inspector.lastDenial" class="meta">暂无拒绝记录（policy_decision allowed=false 时记录）</p>
          <template v-else>
            <p class="deny" role="alert">⛔ {{ inspector.lastDenial.reason }}</p>
            <p class="meta">when：{{ inspector.lastDenial.when }} · seq #{{ inspector.lastDenial.seq }}</p>
          </template>
        </div>
      </div>

      <p v-if="inspector.lastError" class="notice" role="alert">{{ inspector.lastError }}</p>
    </template>
  </section>
</template>

<style scoped>
.inspector {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.bar {
  display: flex;
  align-items: center;
  gap: 12px;
}
h2 {
  margin: 0;
  font-size: 18px;
}
.ws {
  font-size: 12px;
  border: 1px solid #334155;
  border-radius: 999px;
  padding: 2px 10px;
  color: #64748b;
}
.ws[data-state='open'] {
  border-color: #10b981;
  color: #6ee7b7;
}
.ws[data-state='resync'] {
  border-color: #f59e0b;
  color: #fbbf24;
}
.mode-band {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid;
  border-radius: 8px;
  padding: 10px 14px;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.mode-desc {
  color: #94a3b8;
  font-size: 13px;
}
.mode-meta {
  margin-left: auto;
  color: #64748b;
  font-size: 12px;
}
.cards {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.card {
  border: 1px solid #1e293b;
  border-radius: 8px;
  padding: 10px 12px;
  background: #111c31;
}
.card.wide {
  grid-column: span 3;
}
h3 {
  margin: 0 0 6px;
  font-size: 12px;
  color: #94a3b8;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.big {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
}
.meta {
  margin: 4px 0 0;
  color: #64748b;
  font-size: 12px;
}
.fields {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.fields th,
.fields td {
  border-bottom: 1px solid #1e293b;
  text-align: left;
  padding: 4px 8px;
}
.deny {
  margin: 0;
  color: #f87171;
}
.notice {
  margin: 0;
  color: #fbbf24;
  font-size: 13px;
}
.empty {
  color: #64748b;
}
</style>
