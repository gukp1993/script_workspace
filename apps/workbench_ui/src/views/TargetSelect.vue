<script setup lang="ts">
/**
 * 目标选择与会话确认页（UI-003 基础版）。
 *
 * - 目标列表：来自当前项目 targets 领域对象（可执行文件/标题正则）；
 * - 窗口列表：M1 预览版调用 GET /api/v1/windows 展示 PID/HWND；
 *   端点未就绪时显示明确空态（M2 接入 window_service）；
 * - 模式四色风险标识：observe/shadow/dry_run/real_input；
 * - real_input 必须先创建会话（gate_wait），再点"人工确认"放行。
 */
import { computed, onMounted, ref } from 'vue'

import { modeRisk, MODE_OPTIONS } from '../domain/modes'
import { apiFetch } from '../api/client'
import { objectLabel, useProjectStore } from '../stores/project'
import { useSessionStore } from '../stores/session'

const projects = useProjectStore()
const session = useSessionStore()

const selectedTarget = ref('')
const selectedMode = ref<string>('shadow')
const operator = ref('本地操作员')
/** RealInput 人工确认两步制：第一次点击仅进入二次提示，再次点击才放行 */
const confirmArmed = ref(false)

/** 窗口列表（M2 前端点未接入时的空态在模板中呈现） */
interface WindowRow {
  hwnd: number
  pid: number
  exe_name: string
  title: string
}
const windows = ref<WindowRow[]>([])
const windowsNote = ref('')

onMounted(async () => {
  await projects.loadProjects()
  if (projects.currentProjectId) {
    await projects.loadObjects('targets')
  }
  await loadWindows()
})

const targets = computed(() => projects.objects['targets'] ?? [])

const targetsEmpty = computed(() => projects.currentProjectId !== null && targets.value.length === 0)

const modeRiskInfo = computed(() => modeRisk(selectedMode.value))

/** 窗口枚举：M1 控制面暂未提供端点，404 降级为说明文案 */
async function loadWindows(): Promise<void> {
  windows.value = []
  windowsNote.value = ''
  try {
    const data = await apiFetch<{ windows: WindowRow[]; count: number }>('/api/v1/windows')
    windows.value = data.windows
  } catch (exc) {
    windowsNote.value =
      exc instanceof Error && 'status' in exc && (exc as { status?: number }).status === 404
        ? '窗口枚举端点未接入（M2 提供），可先按目标对象选择'
        : exc instanceof Error
          ? `窗口列表获取失败：${exc.message}`
          : '窗口列表获取失败'
  }
}

function selectTarget(id: string): void {
  selectedTarget.value = id
}

async function createSession(): Promise<void> {
  const pid = projects.currentProjectId
  if (!pid || !selectedTarget.value) return
  await session.create(pid, selectedTarget.value, selectedMode.value)
}

async function confirmGate(): Promise<void> {
  await session.confirm(operator.value.trim() || '未署名操作员')
}

/** 人工确认（两步制）：第一击进入二次提示，第二击才真正放行 */
async function requestConfirm(): Promise<void> {
  if (!confirmArmed.value) {
    confirmArmed.value = true
    return
  }
  await confirmGate()
  confirmArmed.value = false
}

function cancelConfirm(): void {
  confirmArmed.value = false
}
</script>

<template>
  <section class="target-select">
    <h2>目标选择</h2>

    <div class="columns">
      <div class="col">
        <h3>项目目标</h3>
        <p v-if="!projects.currentProjectId" class="empty">请先在左侧选择项目</p>
        <p v-else-if="targetsEmpty" class="empty">当前项目没有目标对象</p>
        <ul class="target-list">
          <li v-for="t in targets" :key="objectLabel('targets', t)">
            <button
              class="target"
              :class="{ active: selectedTarget === objectLabel('targets', t) }"
              @click="selectTarget(objectLabel('targets', t))"
            >
              <strong>{{ objectLabel('targets', t) }}</strong>
              <span class="meta">{{ t.executable }} · {{ t.title_regex }}</span>
              <span v-if="t.protected_online" class="protected">受保护在线目标</span>
            </button>
          </li>
        </ul>
      </div>

      <div class="col">
        <h3>窗口列表</h3>
        <p v-if="windowsNote" class="empty">{{ windowsNote }}</p>
        <table v-else-if="windows.length > 0" class="windows">
          <thead>
            <tr>
              <th>PID</th>
              <th>HWND</th>
              <th>进程</th>
              <th>标题</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="w in windows" :key="w.hwnd">
              <td>{{ w.pid }}</td>
              <td>{{ w.hwnd }}</td>
              <td>{{ w.exe_name }}</td>
              <td>{{ w.title }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">未发现可见窗口</p>
      </div>
    </div>

    <h3>执行模式</h3>
    <div class="modes" role="radiogroup" aria-label="执行模式">
      <button
        v-for="mode in MODE_OPTIONS"
        :key="mode"
        role="radio"
        :aria-checked="selectedMode === mode"
        class="mode"
        :class="{ active: selectedMode === mode }"
        :style="{ borderColor: modeRisk(mode).color }"
        @click="selectedMode = mode"
      >
        <span class="dot" :style="{ background: modeRisk(mode).color }" />
        <span>{{ modeRisk(mode).label }}</span>
        <span class="mode-desc">{{ modeRisk(mode).description }}</span>
      </button>
    </div>
    <p class="risk-line">
      当前风险等级：<strong :style="{ color: modeRiskInfo.color }">L{{ modeRiskInfo.level }} · {{ modeRiskInfo.label }}</strong>
      <span v-if="modeRiskInfo.requiresConfirm" class="confirm-hint">（启动前需人工确认）</span>
    </p>

    <div class="session-actions">
      <button
        class="primary"
        :disabled="!selectedTarget || !projects.currentProjectId || !session.canCreate"
        @click="createSession"
      >
        创建会话（{{ modeRiskInfo.label }}）
      </button>

      <template v-if="session.awaitingConfirm">
        <input v-model="operator" aria-label="操作员署名" placeholder="操作员署名" />
        <button v-if="!confirmArmed" class="danger" :disabled="session.busy" @click="requestConfirm">
          人工确认放行 RealInput
        </button>
        <template v-else>
          <span class="confirm-warn" role="alert">
            二次确认：即将向前台窗口注入<strong>真实键鼠输入</strong>，目标窗口将实际收到事件。
          </span>
          <button class="danger armed" :disabled="session.busy" @click="requestConfirm">确认注入（二次确认）</button>
          <button @click="cancelConfirm">取消</button>
        </template>
      </template>
    </div>
    <p v-if="session.error" class="error" role="alert">{{ session.error }}</p>
  </section>
</template>

<style scoped>
.target-select {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 960px;
}
h2 {
  margin: 0;
  font-size: 18px;
}
h3 {
  margin: 0 0 6px;
  font-size: 13px;
  color: #94a3b8;
}
.columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
}
.target-list,
ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.target {
  width: 100%;
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
}
.target.active {
  outline: 2px solid #3b82f6;
}
.meta {
  color: #94a3b8;
  font-size: 12px;
}
.protected {
  color: #fbbf24;
  font-size: 12px;
}
.windows {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.windows th,
.windows td {
  border-bottom: 1px solid #1e293b;
  text-align: left;
  padding: 4px 8px;
}
.modes {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.mode {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 10px;
  border-width: 2px;
  text-align: left;
}
.mode.active {
  background: #1e293b;
  outline: 2px solid #64748b;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.mode-desc {
  font-size: 11px;
  color: #94a3b8;
}
.risk-line {
  margin: 0;
  font-size: 13px;
}
.confirm-hint {
  color: #f87171;
}
.session-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.primary {
  border-color: #3b82f6;
}
.danger {
  border-color: #ef4444;
  color: #fca5a5;
}
.danger.armed {
  background: #7f1d1d;
  font-weight: 600;
}
.confirm-warn {
  color: #fbbf24;
  font-size: 13px;
}
input {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
}
.empty {
  color: #64748b;
  font-size: 13px;
}
.error {
  color: #f87171;
  font-size: 13px;
}
</style>
